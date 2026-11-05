"""诊断子图症状抽取测试：槽位解析、知识库验证与兜底路径。

LLM 用桩模型，数据库走真实本地库验证 symptom_key 反查。
"""

import asyncio
import uuid

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.agent.diagnose as diagnose_module
from app.agent.diagnose import (
    FALLBACK_SYMPTOM_KEY,
    DiagnosisReport,
    SymptomSlots,
    _filter_assertions,
    build_diagnose_graph,
)
from app.config import get_settings


class StubExtractionModel:
    """固定槽位的结构化输出桩；slots=None 模拟 LLM 未返回结构化结果。"""

    def __init__(self, slots: SymptomSlots | None):
        self._slots = slots
        self.received_messages: list[list] = []
        self.received_configs: list = []

    def with_structured_output(self, schema):
        stub = self

        class _Structured:
            async def ainvoke(self, messages, config=None):
                stub.received_messages.append(list(messages))
                # 记录 config 是否透传，锁定 trace 关联不被丢失的回归。
                stub.received_configs.append(config)
                return stub._slots

        return _Structured()


class StubDiagnosisModel:
    """按 schema 分发固定输出的桩：抽取用 SymptomSlots，报告用 DiagnosisReport。"""

    def __init__(self, slots: SymptomSlots, report: DiagnosisReport | None):
        self._slots = slots
        self._report = report
        self.report_prompts: list[list] = []

    def with_structured_output(self, schema):
        stub = self
        if schema is DiagnosisReport:

            class _Report:
                async def ainvoke(self, messages, config=None):
                    stub.report_prompts.append(list(messages))
                    return stub._report

            return _Report()

        class _Slots:
            async def ainvoke(self, messages, config=None):
                return stub._slots

        return _Slots()


def _create_device(client, home_id: int, name: str, device_type: str) -> int:
    resp = client.post(
        f"/homes/{home_id}/devices", json={"name": name, "device_type": device_type}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _run_extract(home_id: int, text: str, model, investigator=None):
    """独立引擎运行诊断子图；结束后同循环销毁，避免连接池跨事件循环复用。"""
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    graph = build_diagnose_graph(chat_model=model, investigator=investigator)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(diagnose_module, "session_factory", session_maker)
    try:

        async def _run():
            try:
                return await graph.ainvoke(
                    {"messages": [HumanMessage(content=text)], "home_id": home_id},
                    config={"configurable": {"thread_id": f"dx-{uuid.uuid4()}"}},
                )
            finally:
                await engine.dispose()

        return asyncio.run(_run())
    finally:
        monkeypatch.undo()


def _scripted_investigator(script: dict[str, list[str]]):
    """按层返回固定证据的排查器桩，并记录调用轨迹供轮次断言。"""
    calls: list[str] = []

    async def _investigate(layer: str, symptom: dict, home_id: int) -> list[str]:
        calls.append(layer)
        return script.get(layer, [])

    _investigate.calls = calls
    return _investigate


def test_extract_resolves_valid_slots(client, home_id):
    """验收口径：客厅灯不亮了 → 槽位解析成功且 symptom_key 通过知识库验证。"""
    light_id = _create_device(client, home_id, "客厅灯", "light")
    gateway_id = _create_device(client, home_id, "家庭网关", "gateway")
    model = StubExtractionModel(
        SymptomSlots(device_name="客厅灯", room="客厅", symptom_key="light_not_on")
    )

    result = _run_extract(home_id, "客厅灯不亮了", model)

    symptom = result["symptom"]
    assert symptom == {
        "device_name": "客厅灯",
        "room": "客厅",
        "device_id": light_id,
        "device_type": "light",
        "domain": "light",
        "symptom_key": "light_not_on",
        "fallback": False,
    }
    # scope 供后续风险判定使用：授权设备覆盖全家庭设备。
    assert sorted(result["scope"]["authorized_device_ids"]) == sorted(
        [light_id, gateway_id]
    )
    assert result["scope"]["home_id"] == home_id
    # prompt 里必须带症状字典供 LLM 选择 key。
    prompt_text = str(model.received_messages[0])
    assert "light_not_on" in prompt_text
    # config 必须透传给 LLM 调用，Langfuse trace 关联依赖它。
    assert model.received_configs[-1] is not None


def test_extract_maps_thermostat_to_ac_domain(client, home_id):
    """空调设备历史类型为 thermostat，须映射到 ac 知识域再验证 key。"""
    _create_device(client, home_id, "卧室空调", "thermostat")
    model = StubExtractionModel(
        SymptomSlots(device_name="卧室空调", room="卧室", symptom_key="ac_not_cooling")
    )

    result = _run_extract(home_id, "卧室空调不制冷", model)

    symptom = result["symptom"]
    assert symptom["domain"] == "ac"
    assert symptom["symptom_key"] == "ac_not_cooling"
    assert symptom["fallback"] is False


def test_extract_falls_back_on_unknown_symptom_key(client, home_id):
    """LLM 编造字典外的 key：落回通用兜底症状并标记 fallback。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubExtractionModel(
        SymptomSlots(device_name="客厅灯", room="客厅", symptom_key="made_up_key")
    )

    result = _run_extract(home_id, "客厅灯怪怪的", model, _scripted_investigator({}))

    symptom = result["symptom"]
    assert symptom["symptom_key"] == FALLBACK_SYMPTOM_KEY
    assert symptom["fallback"] is True
    # 设备解析不受 key 兜底影响。
    assert symptom["domain"] == "light"


def test_extract_falls_back_when_model_returns_none(client, home_id):
    """模型未返回结构化结果（返回 None）：与解析失败同层兜底。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubExtractionModel(None)

    result = _run_extract(home_id, "客厅灯不亮了", model, _scripted_investigator({}))

    symptom = result["symptom"]
    assert symptom["symptom_key"] == FALLBACK_SYMPTOM_KEY
    assert symptom["fallback"] is True


def test_extract_falls_back_on_unknown_device(client, home_id):
    """设备名解析失败：domain 落 unknown，槽位仍保留用户原话。"""
    model = StubExtractionModel(
        SymptomSlots(device_name="不存在的设备", room="", symptom_key="light_not_on")
    )

    result = _run_extract(home_id, "不存在的设备坏了", model)

    symptom = result["symptom"]
    assert symptom["device_id"] is None
    assert symptom["domain"] == "unknown"
    assert symptom["device_name"] == "不存在的设备"
    assert symptom["fallback"] is True


def test_investigation_converges_in_one_round(client, home_id):
    """灯不亮 + 离线证据：交集唯一候选 power_outage 达高置信 → 1 轮收敛。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubExtractionModel(
        SymptomSlots(device_name="客厅灯", symptom_key="light_not_on")
    )
    investigator = _scripted_investigator(
        {"device_status": ["light_offline"], "device_events": [], "gateway_status": []}
    )

    result = _run_extract(home_id, "客厅灯不亮了", model, investigator)

    assert result["investigation_round"] == 1
    assert result["observed_evidence"] == ["light_offline"]
    top = result["candidate_hypotheses"][0]
    assert top["cause_key"] == "power_outage"
    assert top["matched_evidence_count"] == 2
    assert investigator.calls == ["device_status"]
    # 结论节点产出 concluded 结论；报告文本由模板兜底（该测试桩不含报告）。
    diagnosis = result["final_diagnosis"]
    assert diagnosis["status"] == "concluded"
    assert diagnosis["primary_cause_key"] == "power_outage"
    assert diagnosis["report"]


def test_investigation_shrinks_leader_to_two_in_two_rounds(client, home_id):
    """总计划场景：兜底候选全集 → 2 轮区分性证据后领先组收缩到 ≤ 2。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubExtractionModel(
        SymptomSlots(device_name="客厅灯", symptom_key="device_abnormal")
    )
    investigator = _scripted_investigator(
        {"device_status": ["light_buzz"], "device_events": ["light_overheat"]}
    )

    result = _run_extract(home_id, "客厅灯不太对劲", model, investigator)

    assert result["investigation_round"] == 2
    assert result["observed_evidence"] == ["light_buzz", "light_overheat"]
    top_two = {h["cause_key"] for h in result["candidate_hypotheses"][:2]}
    assert top_two == {"switch_overload", "socket_carbonization"}
    assert all(
        h["matched_evidence_count"] == 3 for h in result["candidate_hypotheses"][:2]
    )
    assert investigator.calls == ["device_status", "device_events"]
    assert result["final_diagnosis"]["status"] == "concluded"


def test_investigation_hard_cap_forces_conclusion(client, home_id):
    """三轮均无区分性证据：硬上限强制收口（结论节点属第 7 步）。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubExtractionModel(
        SymptomSlots(device_name="客厅灯", symptom_key="light_not_on")
    )
    investigator = _scripted_investigator({})

    result = _run_extract(home_id, "客厅灯不亮了", model, investigator)

    assert result["investigation_round"] == 3
    assert result["observed_evidence"] == []
    assert investigator.calls == ["device_status", "device_events", "gateway_status"]
    diagnosis = result["final_diagnosis"]
    assert diagnosis["status"] == "unresolved"
    assert "人工" in diagnosis["fix_suggestion"]
    assert "未能定位" in diagnosis["report"]


def test_unknown_domain_skips_investigation(client, home_id):
    """未知域无候选：跳过排查循环，直接进入结论准备。"""
    model = StubExtractionModel(
        SymptomSlots(device_name="不存在的设备", symptom_key="device_abnormal")
    )
    investigator = _scripted_investigator({})

    result = _run_extract(home_id, "不存在的设备坏了", model)

    assert result["candidate_hypotheses"] == []
    assert result["investigation_round"] == 0
    assert investigator.calls == []


def test_filter_assertions_replaces_over_assertions():
    """验收口径：LLM 输出带过度断言词，过滤为非断言表达。"""
    text, replaced = _filter_assertions("一定是网关坏了，已修复。")

    assert text == "可能是网关故障，建议检查。"
    assert ("一定是", "可能是") in replaced
    assert ("坏了", "故障") in replaced
    assert ("已修复", "建议检查") in replaced


def test_filter_assertions_keeps_clean_text():
    text, replaced = _filter_assertions("可能是网关故障，建议检查。")

    assert text == "可能是网关故障，建议检查。"
    assert replaced == []


def test_report_transwrites_and_filters_assertions(client, home_id):
    """LLM 转写命中过滤表：文本进 trace 前被确定性净化。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubDiagnosisModel(
        slots=SymptomSlots(device_name="客厅灯", symptom_key="light_not_on"),
        report=DiagnosisReport(
            report="一定是停电了，已修复。", primary_cause_key="power_outage"
        ),
    )
    investigator = _scripted_investigator({"device_status": ["light_offline"]})

    result = _run_extract(home_id, "客厅灯不亮了", model, investigator)

    diagnosis = result["final_diagnosis"]
    assert diagnosis["status"] == "concluded"
    assert diagnosis["report"] == "可能是停电了，建议检查。"
    # 报告文本追加为 AI 消息，挂进主图后由 to_answer 提取返回。
    assert result["messages"][-1].content == "可能是停电了，建议检查。"
    # 报告 prompt 携带结构化结论 JSON。
    assert "power_outage" in str(model.report_prompts[0])


def test_report_guard_rejects_foreign_conclusion(client, home_id):
    """LLM 复述的结论 key 与输入不一致：判定越权，丢弃转写回退模板。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubDiagnosisModel(
        slots=SymptomSlots(device_name="客厅灯", symptom_key="light_not_on"),
        report=DiagnosisReport(
            report="我判断是外星人干的。", primary_cause_key="alien_attack"
        ),
    )
    investigator = _scripted_investigator({"device_status": ["light_offline"]})

    result = _run_extract(home_id, "客厅灯不亮了", model, investigator)

    report = result["final_diagnosis"]["report"]
    assert "外星人" not in report
    # 模板使用确定性主结论，不引入 LLM 新增内容。
    assert "区域停电" in report


def test_report_falls_back_when_llm_returns_none(client, home_id):
    """LLM 未返回结构化报告：与解析失败同层回退规则模板。"""
    _create_device(client, home_id, "客厅灯", "light")
    model = StubDiagnosisModel(
        slots=SymptomSlots(device_name="客厅灯", symptom_key="light_not_on"),
        report=None,
    )
    investigator = _scripted_investigator({"device_status": ["light_offline"]})

    result = _run_extract(home_id, "客厅灯不亮了", model, investigator)

    report = result["final_diagnosis"]["report"]
    assert "根据排查" in report
    assert "区域停电" in report
