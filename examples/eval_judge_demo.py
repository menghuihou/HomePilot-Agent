"""LLM-as-Judge 最小体验：temperature=0 打分 + 跑两次验证稳定性。

对应第 5 周实施计划「第 1 步：学习 spike」的第 2 件事。

环境约定：
- ANTHROPIC_AUTH_TOKEN：智谱 GLM 的 API Key（Anthropic 兼容 Bearer 鉴权）
- 可选 LLM_MODEL 覆盖默认模型

运行方式：uv run python examples/eval_judge_demo.py

跑完后能回答两个问题：
1. 为什么红线用例（安全边界）不能依赖 LLM judge？
   judge 是概率性的：同输入不保证同输出、可能被措辞"说服"、调用还可能失败；
   红线要求 100% 确定、可复现、零外部依赖，所以只能用确定性断言。
2. 为什么 judge 要固定 temperature=0？
   把采样随机性降到最低，同一输入尽量同一分；评估分数才可跨提交比较。
   注意 temperature=0 不保证绝对一致（score 稳定即可，逐字一致不是目标）。
"""

import os

from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field


# 1) judge 的输出也要结构化：分数 + 一句理由，禁止自由发挥格式。
class JudgeVerdict(BaseModel):
    """单条回复的评审结果。"""

    score: int = Field(ge=1, le=5, description="质量分，1=差，5=好")
    reason: str = Field(description="一句话理由，说明加分或扣分的具体行为")


# 2) criteria 写"行为要求"，不预设答案——这是 golden criteria 的关键纪律。
#    对比第 5 周计划的语义用例：criteria 描述"不得做什么/必须基于什么"，
#    而不是"正确答案是什么"。
CRITERIA = """\
你是家庭助手回复质量评审员。评分标准（1-5 分，只看行为）：
1. 回复中的每个故障判断都必须有依据，不得对未验证的原因下定论；
2. 不得出现"一定是 / 必然 / 肯定"这类确定性断言描述故障原因；
3. 建议可执行、语言口语化。
5 分=完全符合；每违反一条至少扣 2 分。
"""


def build_judge() -> ChatAnthropic:
    """GLM 的 Anthropic 兼容端点；temperature=0 是 judge 的硬性要求。"""
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
        temperature=0,
        max_tokens=1024,
    )


def judge_reply(judge, question: str, reply: str) -> JudgeVerdict:
    """一次 judge 调用：输入固定格式，输出收敛成 JudgeVerdict。"""
    prompt = f"{CRITERIA}\n用户问题：{question}\n助手回复：{reply}\n请按标准打分。"
    return judge.with_structured_output(JudgeVerdict).invoke(prompt)


# 3) 两条对比回复：一条守规矩（"可能"+"建议"），一条越界（确定性断言）。
#    预期：第一条 4-5 分，第二条 ≤3 分且 reason 点名断言词。
QUESTION = "客厅灯不亮了，是不是坏了？"
REPLIES = [
    "可能是设备离线或者灯泡故障，建议先检查灯的供电和开关状态，"
    "再看看网关是否在线，我可以帮你逐步排查。",
    "一定是网关坏了，肯定是你家网络的问题，必然要重启路由器才能修好。",
]


def main() -> None:
    judge = build_judge()
    # 4) 同样的输入跑两遍：直观体验 temperature=0 的稳定性。
    for run in (1, 2):
        print(f"== 第 {run} 次评审（temperature=0）==")
        for index, reply in enumerate(REPLIES, start=1):
            verdict = judge_reply(judge, QUESTION, reply)
            print(f"回复{index} score={verdict.score}/5  reason={verdict.reason}")
        print()
    print("对比两次输出：分数应一致，理由措辞允许微小差异。")
    print("若第二条没有拿到低分，说明 criteria 不够锋利——这正是要人工 review 的地方。")


if __name__ == "__main__":
    main()
