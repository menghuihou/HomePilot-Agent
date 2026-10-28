from fastapi import FastAPI

from app.config import get_settings
from app.routers import devices, homes

settings = get_settings()

app = FastAPI(
    title="HomePilot Agent",
    version="0.1.0",
)

app.include_router(homes.router)
app.include_router(devices.router)


@app.get("/health")
def health() -> dict[str, str]:
    """健康检查，供本地验证和后续编排探活使用。"""
    return {"status": "ok", "env": settings.app_env}
