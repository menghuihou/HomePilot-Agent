import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.diagnose import get_diagnose_graph
from app.config import get_settings
from app.observability import setup_observability
from app.routers import approvals, chat, devices, homes, llm, preferences

settings = get_settings()

# 应用日志统一入口；uvicorn 自身访问日志仍由 uvicorn 配置控制。
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """诊断审批续跑依赖 LangGraph checkpointer 内部表：启动即建表（幂等）。

    独立于 Alembic：这批表属于 LangGraph，不由项目迁移体系管理。
    """
    await get_diagnose_graph()
    yield


application = FastAPI(
    title="HomePilot Agent",
    version="0.1.0",
    lifespan=lifespan,
)

setup_observability(application)
application.include_router(homes.router)
application.include_router(devices.router)
application.include_router(preferences.router)
application.include_router(chat.router)
application.include_router(llm.router)
application.include_router(approvals.router)


@application.get("/health")
def health() -> dict[str, str]:
    """健康检查，供本地验证和后续编排探活使用。"""
    return {"status": "ok", "env": settings.app_env}
