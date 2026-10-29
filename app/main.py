from fastapi import FastAPI

from app.config import get_settings
from app.observability import setup_observability
from app.routers import devices, homes, llm, preferences

settings = get_settings()

application = FastAPI(
    title="HomePilot Agent",
    version="0.1.0",
)

setup_observability(application)
application.include_router(homes.router)
application.include_router(devices.router)
application.include_router(preferences.router)
application.include_router(llm.router)


@application.get("/health")
def health() -> dict[str, str]:
    """健康检查，供本地验证和后续编排探活使用。"""
    return {"status": "ok", "env": settings.app_env}
