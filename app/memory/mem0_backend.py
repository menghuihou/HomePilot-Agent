"""Mem0 长期记忆后端；供应商依赖与 API 只保留在本模块。"""

import asyncio
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Any

from app.config import Settings
from app.schemas import MemoryItem, MemoryQuery, MemoryScope


class Mem0Backend:
    """通过 optional dependency 接入 Mem0；未安装时不能影响 app 导入。"""

    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._client_factory = client_factory

    @classmethod
    def from_settings(cls, settings: Settings) -> Mem0Backend:
        """从应用配置创建；缺少必要配置时给出可排查错误。"""
        if not settings.mem0_api_key.get_secret_value():
            from app.memory import MemoryBackendConfigError

            raise MemoryBackendConfigError(
                "MEMORY_BACKEND=mem0 时必须配置 MEM0_API_KEY"
            )
        return cls(settings)

    @property
    def client(self) -> Any:
        """懒加载供应商 client；未启用 mem0 后端时不产生依赖导入。"""
        if self._client is None:
            self._client = (
                self._client_factory() if self._client_factory else self._build_client()
            )
        return self._client

    def _build_client(self) -> Any:
        """按配置初始化 MemoryClient；API key 只作为构造参数，不进入记忆数据。"""
        try:
            from mem0 import MemoryClient
        except ImportError as exc:
            from app.memory import MemoryBackendUnavailableError

            raise MemoryBackendUnavailableError(
                "mem0ai python package is not installed; "
                "run `uv sync --locked --group memory` before selecting mem0"
            ) from exc

        kwargs: dict[str, Any] = {
            "api_key": self.settings.mem0_api_key.get_secret_value()
        }
        if self.settings.mem0_base_url:
            kwargs["host"] = self.settings.mem0_base_url
        return MemoryClient(**kwargs)

    async def store(self, item: MemoryItem) -> MemoryItem:
        """先精确删除同 scope/key 记录，再写入新值，保证统一契约中的覆盖语义。"""
        existing = await self._list(item.scope.home_id, limit=100)
        for memory in existing:
            metadata = self._metadata(memory)
            if not self._same_store_scope(metadata, item):
                continue
            memory_id = memory.get("id")
            if memory_id is not None:
                await self._run_sdk(partial(self.client.delete, memory_id=memory_id))

        metadata = self._item_metadata(item)
        user_id = self._user_id(item.scope.home_id)
        response = await self._run_sdk(
            lambda: self.client.add(
                messages=item.value,
                user_id=user_id,
                metadata=metadata,
            )
        )
        added = response[0] if isinstance(response, list) and response else response
        record = added if isinstance(added, dict) else {}
        return MemoryItem(
            scope=item.scope,
            key=item.key,
            value=self._memory_value(record) or item.value,
            source=item.source,
            updated_at=self._updated_at(record),
        )

    async def retrieve(self, query: MemoryQuery) -> list[MemoryItem]:
        """按 metadata 二次过滤 Mem0 检索结果；相似度分数不参与权限判断。"""
        search_query = query.key or query.key_prefix or "home preference"
        memories = await self._search(
            query.scope.home_id, query=search_query, limit=query.limit
        )
        items: list[MemoryItem] = []
        for memory in memories:
            metadata = self._metadata(memory)
            if not self._matches_query(metadata, query):
                continue
            value = self._memory_value(memory)
            if not value:
                continue
            items.append(
                MemoryItem(
                    scope=MemoryScope(
                        home_id=query.scope.home_id,
                        member_id=metadata.get("member_id"),
                        device_type=metadata.get("device_type"),
                    ),
                    key=metadata.get("pref_key") or "",
                    value=value,
                    source=metadata.get("source") or "mem0",
                    updated_at=self._updated_at(memory),
                )
            )
        return items[: query.limit]

    async def forget(self, query: MemoryQuery) -> int:
        """按 metadata + key / key_prefix 逐条删除，返回真实删除数量。"""
        memories = await self._list(query.scope.home_id, limit=100)
        deleted = 0
        for memory in memories:
            metadata = self._metadata(memory)
            if not self._matches_query(metadata, query):
                continue
            memory_id = memory.get("id")
            if memory_id is None:
                continue
            await self._run_sdk(partial(self.client.delete, memory_id=memory_id))
            deleted += 1
        return deleted

    async def _list(self, home_id: int, limit: int) -> list[dict]:
        """列出家庭全量候选；upsert / forget 需要确定性 metadata 过滤。"""
        client = self.client
        if not hasattr(client, "get_all"):
            return await self._search(home_id, query="home preference", limit=limit)
        response = await self._run_sdk(
            lambda: client.get_all(user_id=self._user_id(home_id), limit=limit)
        )
        if isinstance(response, list):
            rows: list[Any] = response
        elif isinstance(response, dict):
            rows = response.get("results") or response.get("data") or []
        else:
            rows = []
        return [row for row in rows if isinstance(row, dict)]

    async def _search(self, home_id: int, query: str, limit: int) -> list[dict]:
        """调用 Mem0 search 并把常见返回结构归一为 dict 列表。"""
        response = await self._run_sdk(
            lambda: self.client.search(
                query=query,
                user_id=self._user_id(home_id),
                limit=limit,
            )
        )
        if isinstance(response, list):
            rows: list[Any] = response
        elif isinstance(response, dict):
            rows = response.get("results") or response.get("data") or []
        else:
            rows = []
        return [row for row in rows if isinstance(row, dict)]

    async def _run_sdk(self, operation: Callable[[], object]) -> object:
        """Mem0 Python SDK 是同步 HTTP client；放入线程避免阻塞事件循环。"""
        try:
            return await asyncio.to_thread(operation)
        except ImportError as exc:
            from app.memory import MemoryBackendUnavailableError

            raise MemoryBackendUnavailableError(
                "mem0ai python package is not installed; "
                "run `uv sync --locked --group memory` before selecting mem0"
            ) from exc

    @staticmethod
    def _user_id(home_id: int) -> str:
        """Mem0 user_id 聚合到家庭级；成员隔离由 metadata 过滤兜底。"""
        return f"home:{home_id}"

    @staticmethod
    def _item_metadata(item: MemoryItem) -> dict[str, Any]:
        """写入家庭隔离与精确 key 所需的最小 metadata，不包含密钥。"""
        return {
            "home_id": item.scope.home_id,
            "member_id": item.scope.member_id,
            "device_type": item.scope.device_type,
            "pref_key": item.key,
            "source": item.source,
        }

    @staticmethod
    def _metadata(memory: dict) -> dict[str, Any]:
        metadata = memory.get("metadata")
        return metadata if isinstance(metadata, dict) else {}

    @staticmethod
    def _memory_value(memory: dict) -> str:
        value = memory.get("memory", memory.get("data", ""))
        return value if isinstance(value, str) else ""

    @staticmethod
    def _updated_at(memory: dict) -> datetime | None:
        value = memory.get("updated_at", memory.get("created_at"))
        return value if isinstance(value, datetime) else None

    @staticmethod
    def _same_store_scope(metadata: dict[str, Any], item: MemoryItem) -> bool:
        """覆盖只针对完全相同的 home/member/key；设备域只作为附加 metadata。"""
        return (
            metadata.get("home_id") == item.scope.home_id
            and metadata.get("member_id") == item.scope.member_id
            and metadata.get("pref_key") == item.key
        )

    @classmethod
    def _matches_query(cls, metadata: dict[str, Any], query: MemoryQuery) -> bool:
        """Mem0 是语义召回，返回后必须在服务侧做精确 metadata 权限过滤。"""
        if metadata.get("home_id") != query.scope.home_id:
            return False
        if (
            query.scope.member_id is not None
            and metadata.get("member_id") != query.scope.member_id
        ):
            return False
        pref_key = metadata.get("pref_key")
        if query.key is not None:
            return pref_key == query.key
        if query.key_prefix is not None:
            return isinstance(pref_key, str) and pref_key.startswith(query.key_prefix)
        return True
