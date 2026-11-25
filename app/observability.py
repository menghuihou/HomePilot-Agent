"""可观测性初始化与 span 序列化工具：OpenTelemetry + Langfuse。

统一使用一个 TracerProvider：
- 资源服务名取 OTEL_SERVICE_NAME，Langfuse 中按此名称展示服务；
- 导出器把 span 通过 OTLP HTTP 发送到 Langfuse 自托管实例；
- FastAPIInstrumentor 产生带请求路径的服务端 span；
- langgraph 节点内的 langfuse @observe 会脱离请求 OTel 上下文、生成
  独立根 trace（2026-11-19 实证）；需要在请求 trace 树内串联的节点
  级观测用 OTel span（见 home_mode_graph._traced）。
"""

import base64
import json
import logging
from typing import Any

from fastapi import FastAPI
from langfuse import Langfuse
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import get_settings

logger = logging.getLogger(__name__)


def observation_value(obj: Any) -> Any:
    """Trace 序列化兜底：BaseMessage 转可读结构，其余转字符串。

    LangGraph state 里的 messages 是 BaseMessage 对象，直接 str() 会输出
    ``content='...' additional_kwargs=...`` 这种调试噪声；转成
    {type, content} 后 Langfuse 里一眼可读。其余不可序列化对象
    （pydantic 模型等）降级字符串，保证 json.dumps 不炸。
    """
    if hasattr(obj, "type") and hasattr(obj, "content"):
        return {"type": obj.type, "content": obj.content}
    return str(obj)


def observation_payload(value: Any) -> str:
    """span 入参/出参序列化：JSON 为主，极端对象（循环引用等）回退 repr。"""
    try:
        return json.dumps(value, ensure_ascii=False, default=observation_value)
    except (TypeError, ValueError):
        return repr(value)


def setup_observability(app: FastAPI) -> Langfuse | None:
    """初始化 tracer provider 并为 FastAPI 应用挂载请求插桩。"""
    settings = get_settings()

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )

    # SecretStr 恒为真值且 f-string 渲染为掩码：必须先显式取值再判空 /
    # 拼接，否则空密钥也会走「已配置」分支、Basic 头会带上掩码而非真钥。
    public_key = settings.langfuse_public_key.get_secret_value()
    secret_key = settings.langfuse_secret_key.get_secret_value()
    langfuse_configured = bool(public_key and secret_key)
    langfuse: Langfuse | None = None
    if langfuse_configured:
        credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{settings.langfuse_host}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {credentials}"},
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=settings.langfuse_host,
            tracer_provider=provider,
        )
    else:
        logger.info("Langfuse 未配置，追踪导出已禁用")

    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    return langfuse
