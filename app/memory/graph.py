"""图记忆后端骨架：只固定契约边界，不提供假成功路径。"""

from app.config import Settings
from app.schemas import MemoryItem, MemoryQuery


class GraphMemoryBackend:
    """为后续 Zep / Graphiti 等图记忆保留可替换骨架。"""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    @classmethod
    def from_settings(cls, settings: Settings) -> GraphMemoryBackend:
        """直接调用工厂方法时也必须显式失败，不创建假可用的对象。"""
        if not settings.graph_memory_endpoint:
            from app.memory import MemoryBackendUnavailableError

            raise MemoryBackendUnavailableError(
                "graph memory endpoint is not configured"
            )
        return cls(settings.graph_memory_endpoint)

    async def store(self, item: MemoryItem) -> MemoryItem:
        """显式不可用，避免测试和演示误以为图记忆已接入。"""
        raise NotImplementedError("graph memory backend is scheduled after week 7")

    async def retrieve(self, query: MemoryQuery) -> list[MemoryItem]:
        """显式不可用；后续接入时仍必须返回 MemoryItem 列表。"""
        raise NotImplementedError("graph memory backend is scheduled after week 7")

    async def forget(self, query: MemoryQuery) -> int:
        """显式不可用；后续接入时必须返回真实删除数量。"""
        raise NotImplementedError("graph memory backend is scheduled after week 7")
