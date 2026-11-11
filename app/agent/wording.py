"""巡检 / 诊断子图共用的话术过滤：过度断言词确定性替换（第 3 周引入）。

安全口径：LLM 输出一律过本过滤器，命中「一定 / 必然 / 已修复」类断言词
→ 替换为「可能 / 建议检查」，替换事件进 trace；纯规则实现，零 LLM。
"""

from langfuse import get_client, observe

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
