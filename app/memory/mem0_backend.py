"""Mem0 长期记忆后端骨架；供应商依赖与 API 只保留在本模块。"""

from app.config import Settings
from app.schemas import MemoryItem, MemoryQuery


class Mem0Backend:
    """通过 optional dependency 接入 Mem0；未安装时不能影响 app 导入。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @classmethod
    def from_settings(cls, settings: Settings) -> Mem0Backend:
        """从应用配置创建；缺少必要配置时给出可排查错误。"""
        if not settings.mem0_api_key.get_secret_value():
            from app.memory import MemoryBackendConfigError

            raise MemoryBackendConfigError(
                "MEMORY_BACKEND=mem0 时必须配置 MEM0_API_KEY"
            )
        return cls(settings)

    async def store(self, item: MemoryItem) -> MemoryItem:
        """占位实现；第 5 步会接入 Mem0 并映射 home/member metadata。"""
        raise NotImplementedError("mem0 memory backend will be added in step 5")

    async def retrieve(self, query: MemoryQuery) -> list[MemoryItem]:
        """占位实现；第 5 步会做 metadata 过滤和 DTO 转换。"""
        raise NotImplementedError("mem0 memory backend will be added in step 5")

    async def forget(self, query: MemoryQuery) -> int:
        """占位实现；第 5 步会按 metadata + key 删除。"""
        raise NotImplementedError("mem0 memory backend will be added in step 5")
