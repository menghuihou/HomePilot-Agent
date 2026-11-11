import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agent import chat as chat_module
from app.agent.exceptions import HomeNotFoundError
from app.config import get_settings
from app.observability import setup_observability
from app.routers import approvals, chat, devices, homes, inspections, llm, preferences

settings = get_settings()

# 应用日志统一入口；uvicorn 自身访问日志仍由 uvicorn 配置控制。
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """审批续跑依赖 LangGraph checkpointer 内部表：启动即建表（幂等）。

    独立于 Alembic：这批表属于 LangGraph，不由项目迁移体系管理。
    """
    await chat_module.get_agent_graph()
    yield


application = FastAPI(
    title="HomePilot Agent",
    version="0.1.0",
    lifespan=lifespan,
)


@application.exception_handler(HomeNotFoundError)
async def _home_not_found_handler(_: Request, exc: HomeNotFoundError) -> JSONResponse:
    """把 agent 领域异常翻译成 HTTP 404，内层不感知 FastAPI。"""
    return JSONResponse(status_code=404, content={"detail": str(exc)})


setup_observability(application)
application.include_router(homes.router)
application.include_router(devices.router)
application.include_router(preferences.router)
application.include_router(chat.router)
application.include_router(llm.router)
application.include_router(approvals.router)
application.include_router(inspections.router)


@application.get("/health")
def health() -> dict[str, str]:
    """健康检查，供本地验证和后续编排探活使用。"""
    return {"status": "ok", "env": settings.app_env}
