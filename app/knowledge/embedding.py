"""Embedding 后端契约、OpenAI 兼容 HTTP 实现与离线测试桩。"""

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

import httpx

from app.config import Settings


@runtime_checkable
class EmbeddingBackend(Protocol):
    """RAG 使用的最小向量接口；实现必须校验维度并保持输入顺序。"""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量生成文档向量，输出顺序与输入一一对应。"""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """生成查询向量。"""
        ...


class EmbeddingAPIError(RuntimeError):
    """Embedding 服务返回错误码、结构非法或维度不一致。"""


class OpenAIEmbeddingBackend:
    """OpenAI 兼容 `/embeddings` client；API key 只出现在请求头。"""

    def __init__(
        self,
        settings: Settings,
        *,
        batch_size: int = 32,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("embedding batch size must be positive")
        self.settings = settings
        self.batch_size = batch_size
        self._client = client

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """分批生成文档向量并保持输入顺序。"""
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            vectors.extend(await self._request(batch))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """生成单条查询向量。"""
        return (await self._request([text]))[0]

    async def _request(self, texts: list[str]) -> list[list[float]]:
        """请求一批向量；错误信息只携带状态码，不记录密钥或正文。"""
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self.settings.embedding_timeout_seconds)
            close_client = True
        else:
            close_client = False
        try:
            response = await client.post(
                f"{self.settings.embedding_base_url.rstrip('/')}/embeddings",
                headers={
                    "Authorization": (
                        f"Bearer {self.settings.embedding_api_key.get_secret_value()}"
                    )
                },
                json={
                    "model": self.settings.embedding_model,
                    "input": texts,
                },
            )
            if response.is_error:
                raise EmbeddingAPIError(
                    f"embedding request failed with status {response.status_code}"
                )
            payload = response.json()
            return _parse_embedding_response(
                payload, texts, self.settings.embedding_dim
            )
        finally:
            if close_client:
                await client.aclose()


class FakeEmbeddingBackend:
    """确定性 hash embedding；只用于离线测试，不模拟真实语义质量。"""

    def __init__(self, dimension: int = 64) -> None:
        if dimension < 1:
            raise ValueError("embedding dimension must be positive")
        self.dimension = dimension

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量生成稳定向量。"""
        return [self._embed_one(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        """生成查询向量，与文档向量使用同一 hash 空间。"""
        return self._embed_one(text)

    def _embed_one(self, text: str) -> list[float]:
        """生成 word + character-bigram bag hash，再归一化到单位长度。"""
        normalized = text.lower()
        words = re.findall(r"[a-z0-9_]+|[^\W\d_]+", normalized)
        char_ngrams = {
            normalized[index : index + 2]
            for index in range(max(0, len(normalized) - 1))
            if normalized[index : index + 2].strip()
        }
        vector = [0.0] * self.dimension
        for feature in (*words, *char_ngrams):
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimension
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            # 空 / 全空白文本仍返回确定向量，避免下游除零。
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]


def _parse_embedding_response(
    payload: object, texts: list[str], expected_dimension: int
) -> list[list[float]]:
    """解析并严格校验响应数量、index、向量长度。"""
    if not isinstance(payload, dict):
        raise EmbeddingAPIError("embedding response is not an object")
    items = payload.get("data")
    if not isinstance(items, list) or len(items) != len(texts):
        raise EmbeddingAPIError("embedding item count does not match request")
    vectors: list[list[float] | None] = [None] * len(texts)
    for item in items:
        if not isinstance(item, dict):
            raise EmbeddingAPIError("embedding item is not an object")
        index = item.get("index")
        vector = item.get("embedding")
        if not isinstance(index, int) or not 0 <= index < len(texts):
            raise EmbeddingAPIError("embedding index is invalid")
        if vectors[index] is not None:
            raise EmbeddingAPIError("embedding index is duplicated")
        if not isinstance(vector, list) or not all(
            isinstance(value, int | float) for value in vector
        ):
            raise EmbeddingAPIError("embedding vector is invalid")
        if len(vector) != expected_dimension:
            raise EmbeddingAPIError(
                f"embedding dimension mismatch: expected={expected_dimension}, "
                f"actual={len(vector)}"
            )
        vectors[index] = [float(value) for value in vector]
    if any(vector is None for vector in vectors):
        raise EmbeddingAPIError("embedding response is incomplete")
    return [vector or [] for vector in vectors]
