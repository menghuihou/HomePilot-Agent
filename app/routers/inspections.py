"""巡检接口：家庭页「一键巡检」按钮的轻量入口（复用巡检子图）。

与聊天入口的区别：不走 Router / LLM 意图分类，直接按 report_type
执行巡检或日报；转写走同一 LLM 优先 + 模板兜底管线。
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.inspect import build_inspect_graph
from app.database import get_session
from app.llm_client import build_chat_model
from app.models import Home

router = APIRouter(tags=["inspections"])

# 巡检子图与 LLM 客户端构建成本低但可复用：与 chat 主图保持同一单例口径。
_inspect_graph_singleton: Any | None = None


def get_inspect_graph() -> Any:
    """惰性返回巡检子图单例；测试用 dependency_overrides 替换本依赖。"""
    global _inspect_graph_singleton
    if _inspect_graph_singleton is None:
        _inspect_graph_singleton = build_inspect_graph(chat_model=build_chat_model())
    return _inspect_graph_singleton


class InspectionRunRequest(BaseModel):
    home_id: int
    report_type: Literal["inspection", "daily_report"] = "inspection"


class InspectionRunResponse(BaseModel):
    report_id: int | None
    report_type: str
    total_devices: int
    normal_count: int
    anomalies: list[dict] = Field(default_factory=list)
    summary_text: str


async def get_inspection_run_response(
    home_id: int, report_type: str, graph: Any
) -> InspectionRunResponse:
    """执行巡检子图并组装响应；home 不存在的检查由调用方先行完成。"""
    result = await graph.ainvoke({"home_id": home_id, "report_type": report_type})
    inspect_result = result.get("inspect_result") or {}
    return InspectionRunResponse(
        report_id=inspect_result.get("report_id"),
        report_type=inspect_result.get("report_type", report_type),
        total_devices=inspect_result.get("total_devices", 0),
        normal_count=inspect_result.get("normal_count", 0),
        anomalies=inspect_result.get("anomalies", []),
        summary_text=inspect_result.get("summary_text", ""),
    )


@router.post("/inspections/run", response_model=InspectionRunResponse)
async def run_inspection(
    payload: InspectionRunRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    graph: Annotated[Any, Depends(get_inspect_graph)],
) -> InspectionRunResponse:
    """一键巡检 / 日报：同步执行巡检子图并返回结构化结果。"""
    home = await session.get(Home, payload.home_id)
    if home is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="home not found"
        )
    return await get_inspection_run_response(
        payload.home_id, payload.report_type, graph
    )
