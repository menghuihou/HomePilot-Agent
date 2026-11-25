"""llm_client 单元测试：extract_text_content 截断口径 + invoke_with_fallback 降级链路。"""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage

import app.llm_client as llm_client
from app.llm_client import extract_text_content, invoke_with_fallback


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


# ---------- invoke_with_fallback 降级链路 ----------


class _StubModel:
    """ainvoke 返回预设文本的桩；exc 非空时模拟调用故障，calls 记录次数。"""

    def __init__(self, text: str = "", exc: Exception | None = None):
        self._text = text
        self._exc = exc
        self.calls = 0

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return AIMessage(content=self._text)


def _patch_models(
    monkeypatch, primary: _StubModel, fallback: _StubModel | None = None
) -> None:
    """注入桩模型与固定配置：fallback=None 模拟 yaml 未声明备用模型。"""
    fake_config = SimpleNamespace(
        default_role="light",
        roles={
            "light": SimpleNamespace(
                fallback=SimpleNamespace(model="fallback-model") if fallback else None
            )
        },
    )
    monkeypatch.setattr(llm_client, "build_chat_model", lambda: primary)
    monkeypatch.setattr(llm_client, "_load_models_config_safe", lambda: fake_config)
    if fallback is not None:
        monkeypatch.setattr(llm_client, "build_chat_model_for", lambda spec: fallback)


def test_invoke_primary_success_returns_response(monkeypatch):
    """主模型正常返回：直接采纳，不触发降级。"""
    primary = _StubModel("主模型回复")
    _patch_models(monkeypatch, primary)

    result = asyncio.run(invoke_with_fallback([]))

    assert result.content == "主模型回复"
    assert primary.calls == 1


def test_invoke_falls_back_on_primary_failure(monkeypatch):
    """主模型调用异常：降级备用模型，返回其回复。"""
    primary = _StubModel(exc=RuntimeError("主模型挂"))
    fallback = _StubModel("备用模型回复")
    _patch_models(monkeypatch, primary, fallback)

    result = asyncio.run(invoke_with_fallback([]))

    assert result.content == "备用模型回复"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_invoke_falls_back_on_empty_primary(monkeypatch):
    """主模型空回复（如截断只剩 thinking）：与异常同口径降级。"""
    primary = _StubModel("")
    fallback = _StubModel("备用模型回复")
    _patch_models(monkeypatch, primary, fallback)

    result = asyncio.run(invoke_with_fallback([]))

    assert result.content == "备用模型回复"


def test_invoke_both_fail_returns_empty_for_template_fallback(monkeypatch):
    """两次都失败：返回空 AIMessage，由调用方模板兜底（契约口径）。"""
    primary = _StubModel(exc=RuntimeError("主模型挂"))
    fallback = _StubModel(exc=RuntimeError("备用也挂"))
    _patch_models(monkeypatch, primary, fallback)

    result = asyncio.run(invoke_with_fallback([]))

    assert isinstance(result, AIMessage)
    assert result.content == ""


def test_invoke_without_fallback_config_returns_empty(monkeypatch):
    """yaml 未声明备用模型：主模型失败后直接返回空，不误建备用客户端。"""
    primary = _StubModel(exc=RuntimeError("主模型挂"))
    _patch_models(monkeypatch, primary, fallback=None)

    result = asyncio.run(invoke_with_fallback([]))

    assert result.content == ""
