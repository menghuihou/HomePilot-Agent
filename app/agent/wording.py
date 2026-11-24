"""LLM 输入 / 输出的话术卫生共享件（零 LLM，纯规则）。

- sanitize_prompt_text：用户可控字段进 prompt 前的净化（输入侧）；
- apply_wording_filter / filter_assertions：过度断言词确定性替换（输出侧）。
"""

import re

from langfuse import get_client, observe

# 控制字符（换行 / 制表 / C0 / DEL）：用户字段里的换行会在 System Prompt
# 摘要列表里伪造独立行（prompt 注入形态），统一压成空格内联。
_PROMPT_TEXT_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def sanitize_prompt_text(text: str) -> str:
    """用户可控字段进 prompt 前的净化：控制字符压成空格并去首尾空白。

    设备名 / 偏好等可经 API 写入的数据拼入 System Prompt 时，必须保持
    单行内联——否则「客厅灯\\n【系统指令】…」会在摘要列表里伪造独立
    条目（router / diagnose 抽取 / home_mode 三处共用本函数）。
    """
    return _PROMPT_TEXT_CONTROL_CHARS.sub(" ", text).strip()


# 过度断言词的确定性替换表：长词在前，避免子串二次命中。
ASSERTION_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("一定是", "可能是"),
    ("肯定是", "可能是"),
    ("必然是", "可能是"),
    ("绝对是", "可能是"),
    ("必然", "可能"),
    ("已修复", "建议检查"),
    ("坏了", "故障"),
)


def filter_assertions(text: str) -> tuple[str, list[tuple[str, str]]]:
    """过度断言词确定性替换；返回 (过滤后文本, 替换记录)。"""
    replaced: list[tuple[str, str]] = []
    filtered = text
    for old, new in ASSERTION_REPLACEMENTS:
        if old in filtered:
            filtered = filtered.replace(old, new)
            replaced.append((old, new))
    return filtered, replaced


@observe(name="wording-filter")
def apply_wording_filter(text: str) -> str:
    """措辞过滤观测点：替换事件通过 metadata 进入当前 trace。"""
    filtered, replacements = filter_assertions(text)
    if replacements:
        get_client().update_current_span(
            metadata={"assertion_replacements": replacements}
        )
    return filtered
