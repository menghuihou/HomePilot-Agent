from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """第 1 周只读取应用基础配置；数据库和 Langfuse 配置在后续步骤接入。"""

    model_config = SettingsConfigDict(
        env_file=".env",
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
    model_provider: str = "cloud"
    model_base_url: str = ""
    model_api_key: str = ""
    light_model: str = ""
    # Streamlit 页面调用的 API 地址。
    streamlit_api_base_url: str = "http://127.0.0.1:8000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
