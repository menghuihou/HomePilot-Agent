"""语义 golden 候选生成器（第 5 周 第 5 步）。

读 eval/golden/ 下三个语义集，为 candidate 为空的行生成候选输出并写回：
- generator=llm：调真实管线同款接缝——诊断报告走报告提示词 + 结构化
  输出 + 措辞过滤；聊天回复走聊天系统提示词 + 家庭上下文摘要；
- generator=template：审批文案按前端审批卡片的确定性拼装口径生成。

候选冻结进 golden 后，语义评估（第 6 步）只调 judge；实现改进后用
--force 重新生成并走 PR 更新（与计划 FAQ 的 golden 更新口径一致）。
候选生成 temperature=0.3 仅为降低怪异输出概率，冻结后与生成温度无关。

运行：uv run python eval/generate_candidates.py [--force]
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.chat import _CHAT_SYSTEM_PROMPT
from app.agent.context import HomeContext
from app.agent.diagnose import _REPORT_SYSTEM_PROMPT, DiagnosisReport
from app.agent.router import summarize_home_context
from app.agent.wording import filter_assertions
from app.llm_client import extract_text_content
from eval.deterministic import _make_home_context

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
SEMANTIC_FILES = (
    "diagnosis_semantic.jsonl",
    "approval_semantic.jsonl",
    "chat_semantic.jsonl",
)


def build_llm() -> ChatAnthropic:
    """GLM 的 Anthropic 兼容端点（与 examples/llm_structured_output.py 同款）。"""
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not api_key:
        raise SystemExit("请先设置环境变量 ANTHROPIC_AUTH_TOKEN（智谱 GLM 的 API Key）")
    return ChatAnthropic(
        model=os.environ.get("LLM_MODEL", "glm-4.6"),
        base_url=os.environ.get(
            "ANTHROPIC_BASE_URL",
            "https://open.bigmodel.cn/api/anthropic",
        ),
        api_key=api_key,
        temperature=0.3,
        # GLM 思考块先消耗预算：1024 曾被 thinking 吃满导致正文截断（真实
        # 生产坑，见 app/llm_client.py 注释），候选生成放大预算避免垃圾候选。
        max_tokens=2048,
    )


def approval_copy(row: dict[str, Any]) -> str:
    """审批候选：按 frontend/chat.py 审批卡片的拼装口径生成（确定性）。"""
    if "report_line" in row["input"]:
        return row["input"]["report_line"]
    payload = row["input"]
    return (
        "⛔ 高风险操作待审批\n\n"
        f"动作：`{payload['action']}`　风险等级："
        f"**{payload['risk_level']}**\n\n{payload.get('reason', '')}"
        f"\n\n建议：{payload.get('suggestion', '')}"
    )


async def diagnose_report(llm: ChatAnthropic, payload: dict[str, Any]) -> str:
    """诊断报告候选：与 _report 节点同款提示词 + 结构化输出 + 措辞过滤。"""
    structured = llm.with_structured_output(DiagnosisReport)
    messages = [
        SystemMessage(content=_REPORT_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
    ]
    # 与管线同款重试口径：GLM 推理模型偶发不调结构化工具（返回 None）。
    report = None
    for attempt in (1, 2):
        report = await structured.ainvoke(messages)
        if isinstance(report, DiagnosisReport):
            break
        print(f"结构化输出未收敛（第 {attempt} 次），重试")
    if not isinstance(report, DiagnosisReport):
        raise TypeError("两次结构化输出均未收敛为 DiagnosisReport")
    if report.primary_cause_key != payload.get("primary_cause_key"):
        print(
            f"警告：primary_cause_key 复述不一致 "
            f"({report.primary_cause_key!r})，候选文本进入人工 review"
        )
    return filter_assertions(report.report)[0]


async def chat_reply(llm: ChatAnthropic, context: HomeContext, message: str) -> str:
    """聊天候选：与 reply 节点同款提示词组装；所选场景均无需工具调用，不挂工具。"""
    system = SystemMessage(
        content=_CHAT_SYSTEM_PROMPT.format(home_summary=summarize_home_context(context))
    )
    text = ""
    for attempt in (1, 2):
        response = await llm.ainvoke([system, HumanMessage(content=message)])
        text = extract_text_content(response.content).strip()
        # extract_text_content 对无 text 块的内容回退 str()，raw 形态以 [{ 开头。
        if text and not text.startswith("[{"):
            if "record_preference(" in text:
                # 模型把工具调用伪装成文本输出：场景触发了工具意图，违反
                # 「本集场景均无需工具调用」的前提，候选不可用，需人工换场景。
                print("警告：聊天候选出现文本化工具调用，该场景需人工调整")
            return text
        print(f"聊天候选疑似 thinking 截断（第 {attempt} 次），重试")
    return text


async def generate_llm_row(llm: ChatAnthropic, row: dict[str, Any]) -> str:
    if row["metric"].startswith("diagnosis"):
        return await diagnose_report(llm, row["input"]["payload"])
    return await chat_reply(llm, _make_home_context(), row["input"]["message"])


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


async def main_async(force: bool) -> None:
    llm = build_llm()
    for name in SEMANTIC_FILES:
        path = GOLDEN_DIR / name
        rows = load_rows(path)
        changed = 0
        for row in rows:
            if row["candidate"] and not force:
                continue
            if row["generator"] == "template":
                row["candidate"] = approval_copy(row)
            else:
                row["candidate"] = await generate_llm_row(llm, row)
            write_rows(path, rows)  # 逐行写回：中断不丢已生成进度
            changed += 1
            print(f"[{row['case_id']}] {row['candidate'][:80]}…")
        write_rows(path, rows)
        print(f"== {name}: 共 {len(rows)} 例，本次生成 {changed} 条候选 ==\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="语义 golden 候选生成器")
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已有候选重新生成（默认只填空缺）",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args.force))


if __name__ == "__main__":
    main()
