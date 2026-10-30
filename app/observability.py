"""可观测性初始化：OpenTelemetry + Langfuse。

统一使用一个 TracerProvider：
- 资源服务名取 OTEL_SERVICE_NAME，Langfuse 中按此名称展示服务；
- 导出器把 span 通过 OTLP HTTP 发送到 Langfuse 自托管实例；
- FastAPIInstrumentor 产生带请求路径的服务端 span；
- LLM 路由里的 @observe(as_type="generation") 与服务端 span 同属一条 trace。
"""

import base64
import logging

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


def setup_observability(app: FastAPI) -> Langfuse | None:
    """初始化 tracer provider 并为 FastAPI 应用挂载请求插桩。"""
    settings = get_settings()

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )

    langfuse_configured = bool(
        settings.langfuse_public_key and settings.langfuse_secret_key
    )
    langfuse: Langfuse | None = None
    if langfuse_configured:
        credentials = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
        ).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{settings.langfuse_host}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {credentials}"},
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            tracer_provider=provider,
        )
    else:
        logger.info("Langfuse 未配置，追踪导出已禁用")

    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    return langfuse
