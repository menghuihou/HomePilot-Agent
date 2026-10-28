from fastapi import FastAPI

from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title="HomePilot Agent",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    """健康检查，供本地验证和后续编排探活使用。"""
    return {"status": "ok", "env": settings.app_env}
