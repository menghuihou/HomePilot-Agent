from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 锚定到项目根目录，避免从 examples/ 等子目录启动时按 cwd 找不到配置。
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """第 1 周只读取应用基础配置；数据库和 Langfuse 配置在后续步骤接入。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # Alembic 使用同步驱动连接业务库；缺失时快速失败，避免静默连错库。
    database_url_sync: str
    # FastAPI 运行时使用异步驱动连接业务库。
    database_url: str
    # 模型网关与轻量模型；未配置 API key 时，页面仅生成 trace id 不真实调用。
    # 密钥用 SecretStr：调试打印 / 意外序列化时渲染为掩码而非明文。
    model_provider: str = "cloud"
    model_base_url: str = ""
    model_api_key: SecretStr = SecretStr("")
    light_model: str = ""
    # 模型适配层 yaml 路径（第 8 步）：LLM_MODELS_YAML 环境变量可覆盖。
    llm_models_yaml: str = ""
    # 第 7 周记忆后端：database 是零外部依赖基线，mem0 可选，graph 暂只留契约。
    memory_backend: Literal["database", "mem0", "graph"] = "database"
    mem0_base_url: str = ""
    mem0_api_key: SecretStr = SecretStr("")
    graph_memory_endpoint: str = ""
    # 第 7 周故障知识 RAG 配置；embedding 关闭时仍可建 chunk 与 keyword 索引。
    knowledge_version: str = "2026.11-seed-v1"
    knowledge_top_k: int = Field(default=4, ge=1, le=20)
    embedding_enabled: bool = False
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = "Qwen3-Embedding-0.6B"
    embedding_dim: int = Field(default=1024, ge=1)
    embedding_timeout_seconds: int = Field(default=10, ge=1, le=120)
    # Streamlit 页面调用的 API 地址。
    streamlit_api_base_url: str = "http://127.0.0.1:8000"
    # 可观测性：OTel 资源服务名与 Langfuse 接入。
    otel_service_name: str = "homepilot-agent"
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def validate_week7_backends(self) -> Settings:
        """尽早暴露第 7 周可选后端配置错误，避免请求阶段才失败。"""
        if self.memory_backend == "graph" and not self.graph_memory_endpoint:
            raise ValueError("MEMORY_BACKEND=graph 时必须配置 GRAPH_MEMORY_ENDPOINT")
        if self.memory_backend == "mem0" and not self.mem0_api_key.get_secret_value():
            raise ValueError("MEMORY_BACKEND=mem0 时必须配置 MEM0_API_KEY")
        if self.embedding_enabled:
            missing = [
                name
                for name, value in (
                    ("EMBEDDING_BASE_URL", self.embedding_base_url),
                    ("EMBEDDING_MODEL", self.embedding_model),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "EMBEDDING_ENABLED=true 时缺少必填配置：" + "、".join(missing)
                )
        return self


@lru_cache
def get_settings() -> Settings:
    # database_url / database_url_sync 由环境变量提供，静态检查不可见。
    return Settings()  # type: ignore[call-arg]
