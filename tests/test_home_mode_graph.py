"""家庭模式子图测试：查询回复、自动迁移、睡眠静音、审批挂起与恢复。

MCP 状态查询走真实灯光 Server（与 diagnose 测试同款依赖真实本地库），
执行器注入桩并真实落库设备事件，使「执行 → 回读」链路完整可验证；
LLM 摘要注入桩，保证测试零真实模型调用。审批挂起的恢复必须发生在
同一 checkpointer 实例内（MemorySaver 不跨实例，生产路径为 Postgres）。
"""

import asyncio
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from psycopg.types.json import Json
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agent.diagnose as diagnose_module
import app.agent.home_mode_graph as home_mode_module
from app.agent.chat import build_agent_graph
from app.agent.home_mode import REASON_MEDIA_MUTED, REASON_OFFLINE
from app.agent.home_mode_graph import (
    _build_summarizer,
    _build_tool_args,
    build_home_mode_graph,
)
from app.config import BASE_DIR, get_settings
from app.main import application
from app.mcp.client import McpToolClient
from app.routers import chat as chat_api
from tests.test_agent_chat import StubChatModel, StubRouterModel
from tests.test_chat_api import _parse_sse


def _conninfo() -> str:
    return get_settings().database_url_sync.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def _db_execute(query: str, params: tuple = (), fetch: bool = False):
    with psycopg.connect(_conninfo()) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows


def _seed_policy(home_id: int, mode: str, rules: dict) -> None:
    _db_execute(
        """
        INSERT INTO home_policies (home_id, mode, device_rules,
                                   synthetic, source_type)
        VALUES (%s, %s, %s, TRUE, 'test')
        """,
        (home_id, mode, Json(rules)),
    )


def _create_device(client, home_id: int, name: str, device_type: str) -> int:
    resp = client.post(
        f"/homes/{home_id}/devices", json={"name": name, "device_type": device_type}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _append_event(device_id: int, event_type: str, payload: dict) -> None:
    _db_execute(
        """
        INSERT INTO device_events (device_id, event_type, payload,
                                   synthetic, source_type)
        VALUES (%s, %s, %s, TRUE, 'test')
        """,
        (device_id, event_type, Json(payload)),
    )


def _stub_executor(calls: list):
    """带真实副作用的执行器桩：灯动作落库事件，让回读能验证生效。"""

    async def _executor(action: dict) -> dict:
        calls.append(action)
        device_id = action.get("device_id")
        if action["action"] == "turn_light_off" and device_id:
            _append_event(int(device_id), "power_off", {"brightness": 0})
        if action["action"] == "turn_light_on" and device_id:
            _append_event(int(device_id), "power_on", {"brightness": 80})
        return {"ok": True, "detail": "ok"}

    return _executor


def _fake_summarizer(text: str):
    async def _summarize(summary: dict) -> str:
        return text

    return _summarize


def _cleanup_approvals(thread_id: str) -> None:
    _db_execute("DELETE FROM approvals WHERE thread_id = %s", (thread_id,))


def _run_home_mode(
    home_id: int,
    text: str,
    *,
    executor=None,
    summarizer=None,
    use_checkpointer=False,
    resume=None,
):
    """独立引擎运行模式子图；返回 (挂起前结果, 最终结果, thread_id)。

    resume 非空时在同一图实例内挂起后恢复（MemorySaver 不跨实例）。
    审批助手内部使用 diagnose.session_factory 落审计行，与子图自身的
    session_factory 一并替换为本次独立引擎（跨事件循环约定）。
    审批清理由调用方按返回的 thread_id 显式执行。
    """
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    # MCP 客户端按文档约定每轮独立新建：模块单例绑定启动时的事件循环，
    # 跨 asyncio.run 复用会拿到死循环会话（挂起 / CancelledError）。
    light_client = McpToolClient(
        command="uv",
        args=["run", "python", "mcp_servers/light_server.py"],
        cwd=BASE_DIR,
    )

    async def _get_light_tools():
        return await light_client.ensure_started()

    async def _get_ac_tools():
        # ac_server.py 第 6 步交付；未交付前启动失败，由执行器兜底。
        return await home_mode_module.get_ac_tools()

    async def _fetch_status(device_id: int) -> dict:
        tools = {tool.name: tool for tool in await _get_light_tools()}
        tool = tools.get("get_device_status")
        if tool is None:
            return {"error": "MCP 工具 get_device_status 未注册"}
        result = await tool.ainvoke({"device_id": str(device_id)})
        return result if isinstance(result, dict) else {"error": "MCP 返回结构异常"}

    graph = build_home_mode_graph(
        summarizer=summarizer,
        executor=executor,
        checkpointer=MemorySaver() if use_checkpointer else None,
    )
    patch = pytest.MonkeyPatch()
    patch.setattr(home_mode_module, "session_factory", session_maker)
    patch.setattr(diagnose_module, "session_factory", session_maker)
    patch.setattr(home_mode_module, "get_light_tools", _get_light_tools)
    patch.setattr(home_mode_module, "get_ac_tools", _get_ac_tools)
    patch.setattr(home_mode_module, "fetch_device_status", _fetch_status)
    thread_id = f"hm-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    try:

        async def _run():
            try:
                first = await graph.ainvoke(
                    {"messages": [HumanMessage(content=text)], "home_id": home_id},
                    config=config,
                )
                final = first
                if resume is not None:
                    final = await graph.ainvoke(Command(resume=resume), config=config)
                return first, final
            finally:
                await engine.dispose()
                await light_client.aclose()

        first, final = asyncio.run(_run())
        return first, final, thread_id
    finally:
        patch.undo()


def test_query_mode_replies_deterministically(client, home_id):
    """「现在是什么模式」：确定性关键词不命中 → 回退查询回复（零 LLM）。"""
    thread_id = None
    try:
        result, _, thread_id = _run_home_mode(home_id, "现在是什么模式")
    finally:
        if thread_id:
            _cleanup_approvals(thread_id)

    assert result["answer"] == "当前家庭模式是：normal。"
    assert any(getattr(m, "type", "") == "ai" for m in result["messages"])


def test_away_migration_auto_executes_and_persists(client, home_id):
    """「我出门了」全低风险：自动执行 + 模式历史与偏好同步落库。"""
    device_id = _create_device(client, home_id, "客厅灯", "light")
    _append_event(device_id, "power_on", {"brightness": 80})
    _seed_policy(
        home_id,
        "away",
        {
            "by_device_type": {"light": {"power": "off", "risk": "low"}},
            "exclusions": [],
            "conflict_checks": [],
        },
    )
    calls: list = []
    thread_id = None
    try:
        result, _, thread_id = _run_home_mode(
            home_id,
            "我出门了",
            executor=_stub_executor(calls),
            summarizer=_fake_summarizer("迁移完成（测试摘要）。"),
        )

        assert result["answer"] == "迁移完成（测试摘要）。"
        assert result["approval_decision"] == "auto"
        assert [item["action"] for item in result["execution_result"]] == [
            "turn_light_off"
        ]
        assert all(item["ok"] for item in result["execution_result"])
        assert result["unresolved_devices"] == []

        history = _db_execute(
            "SELECT mode FROM home_modes WHERE home_id = %s AND ended_at IS NULL",
            (home_id,),
            fetch=True,
        )
        assert [row[0] for row in history] == ["away"]
        pref = _db_execute(
            "SELECT pref_value FROM preferences WHERE home_id = %s AND pref_key = 'home_mode'",
            (home_id,),
            fetch=True,
        )
        assert pref[0][0] == "away"
    finally:
        if thread_id:
            _cleanup_approvals(thread_id)


def test_sleep_migration_mutes_media_device(client, home_id):
    """「准备睡觉」：媒体设备自动播放被静音转待确认，灯光正常执行。"""
    light_id = _create_device(client, home_id, "卧室灯", "light")
    _append_event(light_id, "power_on", {"brightness": 70})
    speaker_id = _create_device(client, home_id, "客厅音箱", "media")
    _append_event(speaker_id, "heartbeat", {"online": True})
    _seed_policy(
        home_id,
        "sleep",
        {
            "by_device_type": {
                "light": {"power": "off", "risk": "low"},
                "media": {"power": "on", "risk": "low"},
            },
            "mute_media": True,
            "allow_night_light": True,
            "exclusions": [],
            "conflict_checks": [],
        },
    )
    calls: list = []
    thread_id = None
    try:
        result, _, thread_id = _run_home_mode(
            home_id,
            "准备睡觉",
            executor=_stub_executor(calls),
            summarizer=_fake_summarizer("睡眠迁移完成（测试摘要）。"),
        )

        muted = [
            item
            for item in result["blocked_changes"]
            if item["device_id"] == str(speaker_id)
        ]
        assert [item["reason"] for item in muted] == [REASON_MEDIA_MUTED]
        # 静音动作不得执行；灯光关闭照常执行并回读生效。
        assert all(action["action"] != "set_media_power" for action in calls)
        assert [item["action"] for item in result["execution_result"]] == [
            "turn_light_off"
        ]
        assert result["unresolved_devices"] == []
    finally:
        if thread_id:
            _cleanup_approvals(thread_id)


def test_high_risk_lock_interrupts_then_resume_approves(client, home_id):
    """离家落锁高风险：interrupt 挂起 → 批准恢复 → 执行落库可查。"""
    device_id = _create_device(client, home_id, "客厅灯", "light")
    _append_event(device_id, "power_on", {"brightness": 80})
    _seed_policy(
        home_id,
        "away",
        {
            "by_device_type": {"light": {"power": "off", "risk": "low"}},
            "lock": {"target": "locked", "risk": "high"},
            "exclusions": [],
            "conflict_checks": [],
        },
    )
    calls: list = []
    thread_id = None
    try:
        first, final, thread_id = _run_home_mode(
            home_id,
            "我出门了",
            executor=_stub_executor(calls),
            summarizer=_fake_summarizer("迁移完成（测试摘要）。"),
            use_checkpointer=True,
            resume={"decision": "approved", "reason": ""},
        )

        # 第一次运行在 gate 挂起，灯光动作未执行；批准后完整走完。
        assert "__interrupt__" in first
        assert final["answer"] == "迁移完成（测试摘要）。"
        assert [item["action"] for item in final["execution_result"]] == [
            "turn_light_off",
            "lock_door",
        ]
        approval = _db_execute(
            "SELECT decision FROM approvals WHERE thread_id = %s",
            (thread_id,),
            fetch=True,
        )
        assert [row[0] for row in approval] == ["approved"]
        history = _db_execute(
            "SELECT mode FROM home_modes WHERE home_id = %s AND ended_at IS NULL",
            (home_id,),
            fetch=True,
        )
        assert [row[0] for row in history] == ["away"]
    finally:
        _cleanup_approvals(thread_id)


def test_high_risk_reject_cancels_migration(client, home_id):
    """拒绝审批：迁移取消，不执行动作、不落模式历史。"""
    device_id = _create_device(client, home_id, "客厅灯", "light")
    _append_event(device_id, "power_on", {"brightness": 80})
    _seed_policy(
        home_id,
        "away",
        {
            "by_device_type": {"light": {"power": "off", "risk": "low"}},
            "lock": {"target": "locked", "risk": "high"},
            "exclusions": [],
            "conflict_checks": [],
        },
    )
    calls: list = []
    thread_id = None
    try:
        first, final, thread_id = _run_home_mode(
            home_id,
            "我出门了",
            executor=_stub_executor(calls),
            summarizer=_fake_summarizer("不应被调用。"),
            use_checkpointer=True,
            resume={"decision": "rejected", "reason": "再等等"},
        )

        assert "__interrupt__" in first
        assert final["answer"] == "已取消本次模式迁移，家庭保持当前模式。"
        assert calls == []
        history = _db_execute(
            "SELECT count(*) FROM home_modes WHERE home_id = %s",
            (home_id,),
            fetch=True,
        )
        assert history[0][0] == 0
        pref = _db_execute(
            "SELECT count(*) FROM preferences WHERE home_id = %s AND pref_key = 'home_mode'",
            (home_id,),
            fetch=True,
        )
        assert pref[0][0] == 0
    finally:
        if thread_id:
            _cleanup_approvals(thread_id)


def test_approval_audit_records_device_level_target(client, home_id):
    """设备级代表动作（未入表域 fail-safe high）：审计行记录目标设备 id。"""
    speaker_id = _create_device(client, home_id, "客厅音箱", "media")
    _append_event(speaker_id, "heartbeat", {"online": True})
    _seed_policy(
        home_id,
        "sleep",
        {
            # media 域动作用户规则表：set_media_power 走未知动作 fail-safe
            # 兜底 high + 强制审批，成为设备级代表性动作。
            "by_device_type": {"media": {"power": "on", "risk": "low"}},
            "exclusions": [],
            "conflict_checks": [],
        },
    )
    calls: list = []
    thread_id = None
    try:
        _, final, thread_id = _run_home_mode(
            home_id,
            "准备睡觉",
            executor=_stub_executor(calls),
            summarizer=_fake_summarizer("迁移完成（测试摘要）。"),
            use_checkpointer=True,
            resume={"decision": "approved", "reason": ""},
        )

        assert final["approval_decision"] == "approved"
        audit = _db_execute(
            "SELECT action, target_device_id FROM approvals WHERE thread_id = %s",
            (thread_id,),
            fetch=True,
        )
        assert audit[0][0] == "set_media_power"
        assert audit[0][1] == speaker_id
    finally:
        if thread_id:
            _cleanup_approvals(thread_id)


def test_policy_missing_replies_gracefully(client, home_id):
    """未配置模式策略：确定性回复引导，不进入迁移流程。"""
    thread_id = None
    try:
        result, _, thread_id = _run_home_mode(home_id, "我出门了")
    finally:
        if thread_id:
            _cleanup_approvals(thread_id)

    assert "还没有配置「离家」模式" in result["answer"]


def test_away_blocks_ac_action_when_ac_domain_unavailable(client, home_id):
    """ac 域状态查询失败（Server 第 6 步交付）：离线兜底待确认而非盲发。"""
    light_id = _create_device(client, home_id, "客厅灯", "light")
    _append_event(light_id, "power_on", {"brightness": 80})
    ac_id = _create_device(client, home_id, "客厅空调", "ac")
    _append_event(ac_id, "energy_report", {"kwh": 1.0})
    _seed_policy(
        home_id,
        "away",
        {
            "by_device_type": {
                "light": {"power": "off", "risk": "low"},
                "ac": {"power": "off", "risk": "medium"},
            },
            "exclusions": [],
            "conflict_checks": [],
        },
    )
    calls: list = []
    thread_id = None
    try:
        result, _, thread_id = _run_home_mode(
            home_id,
            "我出门了",
            executor=_stub_executor(calls),
            summarizer=_fake_summarizer("迁移完成（测试摘要）。"),
        )

        # ac 状态不可查 → 离线兜底进待确认；灯光照常执行。
        blocked = [
            item
            for item in result["blocked_changes"]
            if item["device_id"] == str(ac_id)
        ]
        assert [item["reason"] for item in blocked] == [REASON_OFFLINE]
        assert [item["action"] for item in result["execution_result"]] == [
            "turn_light_off"
        ]
    finally:
        if thread_id:
            _cleanup_approvals(thread_id)


def test_all_blocked_reply_lists_devices_and_reasons(client, home_id):
    """全部动作被拦：回复按原因分组逐台列出设备，不再只报数量。"""
    ac_id = _create_device(client, home_id, "客厅空调", "ac")
    _append_event(ac_id, "energy_report", {"kwh": 1.0})
    _seed_policy(
        home_id,
        "away",
        {
            "by_device_type": {"ac": {"power": "off", "risk": "medium"}},
            "exclusions": [],
            "conflict_checks": [],
        },
    )
    thread_id = None
    try:
        result, _, thread_id = _run_home_mode(home_id, "我出门了")
    finally:
        if thread_id:
            _cleanup_approvals(thread_id)

    # ac 域 Server 未交付 → 状态不可查 → 离线兜底 → 全部待确认。
    assert result["answer"] == (
        "本次迁移有 1 项被暂停待确认：客厅空调"
        "（设备离线，暂停执行，标记待确认）。暂无可自动执行的动作。"
    )


def test_main_graph_routes_home_mode_into_subgraph(client, home_id):
    """主图挂载验证：home_mode 意图经条件边进入子图（查询路径零 LLM）。"""
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    graph = build_agent_graph(
        router_model=StubRouterModel("home_mode"),
        chat_model=StubChatModel([]),
        home_mode_graph=build_home_mode_graph(),
    )
    patch = pytest.MonkeyPatch()
    patch.setattr("app.agent.chat.session_factory", session_maker)
    patch.setattr(home_mode_module, "session_factory", session_maker)
    try:

        async def _run():
            try:
                return await graph.ainvoke(
                    {
                        "messages": [HumanMessage(content="现在是什么模式")],
                        "home_id": home_id,
                    },
                    config={
                        "configurable": {"thread_id": f"hm-main-{uuid.uuid4().hex[:8]}"}
                    },
                )
            finally:
                await engine.dispose()

        result = asyncio.run(_run())
    finally:
        patch.undo()

    assert result["intent"] == "home_mode"
    assert result["answer"] == "当前家庭模式是：normal。"


def test_build_tool_args_passes_brightness_for_light_on():
    """夜间低亮度：亮度目标随 turn_light_on 透传；无亮度动作只带设备。"""
    args = _build_tool_args(
        {
            "device_id": "1",
            "action": "turn_light_on",
            "target": {"power": "on", "brightness": 20},
        }
    )
    assert args == {"device_id": "1", "brightness": 20}
    assert _build_tool_args(
        {"device_id": "1", "action": "turn_light_off", "target": {"power": "off"}}
    ) == {"device_id": "1"}


def test_summarizer_falls_back_to_template_when_llm_empty(client, home_id):
    """LLM 返回空正文（截断等）：降级确定性模板，绝不输出空文本。"""

    class _EmptyModel:
        async def ainvoke(self, messages, config=None):
            return SimpleNamespace(content=[])

    summarize = _build_summarizer(chat_model=_EmptyModel())
    summary = {
        "target_mode": "night",
        "total_count": 2,
        "applied_count": 0,
        "blocked": [
            {"device_name": "客厅空调", "reason": "设备离线，暂停执行，标记待确认"}
        ],
        "unresolved": [],
        "failed": [],
    }

    text = asyncio.run(summarize(summary))

    assert "夜间" in text
    assert "客厅空调" in text


def test_stream_interrupt_done_answer_not_stale(client, home_id):
    """回归：挂起时 done.answer 不得回读线程旧值。

    复现路径（用户实测）：同线程先问「当前模式」，再发「我出门了」——
    gate 挂起后 done 事件曾把上一轮查询回复冒充本轮结果。
    """
    device_id = _create_device(client, home_id, "客厅灯", "light")
    _append_event(device_id, "power_on", {"brightness": 80})
    _seed_policy(
        home_id,
        "away",
        {
            "by_device_type": {"light": {"power": "off", "risk": "low"}},
            "lock": {"target": "locked", "risk": "high"},
            "exclusions": [],
            "conflict_checks": [],
        },
    )
    calls: list = []
    graph = build_agent_graph(
        router_model=StubRouterModel("home_mode"),
        chat_model=StubChatModel([]),
        home_mode_graph=build_home_mode_graph(
            summarizer=_fake_summarizer("迁移完成（测试摘要）。"),
            executor=_stub_executor(calls),
            checkpointer=MemorySaver(),
        ),
    )
    application.dependency_overrides[chat_api.get_agent_graph] = lambda: graph
    thread_id = f"hm-stream-{uuid.uuid4().hex[:8]}"
    try:
        # 第一轮：同线程先查询，在线程状态里留下旧 answer。
        with client.stream(
            "POST",
            "/chat/stream",
            json={
                "home_id": home_id,
                "message": "现在是什么模式",
                "thread_id": thread_id,
            },
        ) as response:
            assert response.status_code == 200
            first = _parse_sse("".join(response.iter_text()))
        assert first[-1]["answer"] == "当前家庭模式是：normal。"

        # 第二轮：同线程触发迁移 → gate 挂起 → done.answer 必须为空。
        with client.stream(
            "POST",
            "/chat/stream",
            json={"home_id": home_id, "message": "我出门了", "thread_id": thread_id},
        ) as response:
            assert response.status_code == 200
            second = _parse_sse("".join(response.iter_text()))
    finally:
        application.dependency_overrides.pop(chat_api.get_agent_graph, None)
        _cleanup_approvals(thread_id)

    approval = [e for e in second if e["event"] == "approval_required"]
    done = [e for e in second if e["event"] == "done"]
    assert approval and approval[0]["action"] == "lock_door"
    assert done and done[0]["answer"] == "", done
