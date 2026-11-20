"""extract_text_content 单元测试：多块内容只取正文，截断不泄漏 thinking。"""

from app.llm_client import extract_text_content


def test_plain_string_passes_through():
    assert extract_text_content("普通回复") == "普通回复"


def test_mixed_blocks_extracts_text_only():
    content = [
        {"type": "thinking", "thinking": "推理过程", "signature": "sig"},
        {"type": "text", "text": "最终回复"},
    ]
    assert extract_text_content(content) == "最终回复"


def test_thinking_only_truncation_returns_empty():
    """max_tokens 截断只剩 thinking / 签名块：返回空串，不回退 str(content)。"""
    content = [
        {"type": "thinking", "thinking": "很长的推理过程", "signature": "sig"},
        {"index": 0, "signature": "!abc", "type": "signature"},
    ]
    result = extract_text_content(content)

    assert result == ""
    assert "推理过程" not in result


def test_non_dict_blocks_skipped():
    content = ["裸字符串块", {"text": "正文"}]
    assert extract_text_content(content) == "正文"


def test_non_list_non_string_returns_empty():
    assert extract_text_content(12345) == ""
    assert extract_text_content(None) == ""
