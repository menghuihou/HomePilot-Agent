"""故障知识三路检索：structured / keyword / dense。"""

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.knowledge.embedding import EmbeddingBackend
from app.knowledge.fusion import fuse_candidates, validate_citations
from app.knowledge.types import ScoredHit
from app.models import FaultSymptom, KnowledgeChunk, KnowledgeSource
from app.schemas import Citation, RetrievalHit, RetrievalResult

logger = logging.getLogger(__name__)
RECALL_LIMIT = 30


@dataclass(slots=True)
class KnowledgeRetrievalStats:
    """检索通道命中数与降级标签，供 Langfuse 和诊断状态使用。"""

    query: str
    structured_hits: int = 0
    keyword_hits: int = 0
    dense_hits: int = 0
    final_hits: int = 0
    fallbacks: list[str] = field(default_factory=list)


def _hit(
        chunk: KnowledgeChunk,
        source: KnowledgeSource,
        score: float,
) -> RetrievalHit:
    """把 ORM 行转换为带完整 citation 的检索项。"""
    metadata = dict(chunk.chunk_metadata)
    metadata["chunk_key"] = chunk.chunk_key
    metadata["source_key"] = source.source_key
    citation = Citation(
        source_id=int(source.id or 0),
        chunk_id=int(chunk.id or 0),
        source_key=source.source_key,
        title=chunk.title,
        chunk_index=int(metadata.get("chunk_index", 0)),
        device_type=source.device_type,
        knowledge_version=source.knowledge_version,
        score=max(0.0, min(1.0, score)),
    )
    return RetrievalHit(
        citation=citation, chunk_text=chunk.chunk_text, metadata=metadata
    )


def _base_filters(
        chunk: Any,
        source: Any,
        device_type: str,
        knowledge_version: str,
) -> list[Any]:
    """所有通道共享的强制权限与版本过滤，不允许调用方覆盖。"""
    return [
        source.device_type == device_type,
        source.knowledge_version == knowledge_version,
        chunk.knowledge_version == knowledge_version,
        chunk.status == "active",
    ]


def _contains(value: str) -> str:
    """转义 ILIKE 通配符，防止查询字符串改变过滤语义。"""
    return f"%{value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"


async def _structured_recall(
        session_factory: async_sessionmaker[AsyncSession],
        query: str,
        device_type: str,
        knowledge_version: str,
        symptom_keys: list[str] | None,
        cause_keys: list[str] | None,
        limit: int,
) -> list[ScoredHit]:
    """按已验证 symptom / cause key 反查知识 chunk。"""
    keys = symptom_keys
    if keys is None:
        english_keys = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", query)
        async with session_factory() as session:
            result = await session.execute(
                select(FaultSymptom).where(
                    FaultSymptom.device_type == device_type,
                    or_(
                        FaultSymptom.symptom_key.in_(english_keys),
                        FaultSymptom.description.ilike(_contains(query)),
                    ),
                ),
            )
            keys = [row.symptom_key for row in result.scalars()]

    conditions = []
    if keys:
        conditions.append(KnowledgeChunk.chunk_metadata["symptom_keys"].contains(keys))
    if cause_keys:
        conditions.extend(
            [KnowledgeChunk.chunk_metadata["cause_key"] == key for key in cause_keys]
        )
    if not conditions:
        return []

    async with session_factory() as session:
        result = await session.execute(
            select(KnowledgeChunk, KnowledgeSource)
            .join(KnowledgeSource, KnowledgeChunk.source_id == KnowledgeSource.id)
            .where(
                *_base_filters(
                    KnowledgeChunk,
                    KnowledgeSource,
                    device_type,
                    knowledge_version,
                ),
                or_(*conditions),
            )
            .order_by(KnowledgeChunk.id)
            .limit(limit)
        )
        rows: list[Any] = list(result.all())
        return [
            ScoredHit(
                hit=_hit(chunk, source, score=1 / (rank + 1)),
                rank=rank,
                score=1 / (rank + 1),
                channel="structured",
            )
            for rank, (chunk, source) in enumerate(rows, start=1)
        ]


async def _keyword_recall(
        session_factory: async_sessionmaker[AsyncSession],
        query: str,
        device_type: str,
        knowledge_version: str,
        limit: int,
) -> list[ScoredHit]:
    """使用全文检索，型号 / 错误码 / 动作词再通过 ILIKE 兜底。"""
    ts_query = func.websearch_to_tsquery("simple", query)
    async with session_factory() as session:
        result = await session.execute(
            select(KnowledgeChunk, KnowledgeSource)
            .join(KnowledgeSource, KnowledgeChunk.source_id == KnowledgeSource.id)
            .where(
                *_base_filters(
                    KnowledgeChunk,
                    KnowledgeSource,
                    device_type,
                    knowledge_version,
                ),
                or_(
                    KnowledgeChunk.search_tsv.op("@@")(ts_query),
                    KnowledgeChunk.search_text.ilike(_contains(query)),
                ),
            )
            .order_by(
                func.ts_rank(KnowledgeChunk.search_tsv, ts_query).desc(),
                KnowledgeChunk.id,
            )
            .limit(limit)
        )
        rows: list[Any] = list(result.all())
        return [
            ScoredHit(
                hit=_hit(chunk, source, score=1 / (rank + 1)),
                rank=rank,
                score=1 / (rank + 1),
                channel="keyword",
            )
            for rank, (chunk, source) in enumerate(rows, start=1)
        ]


async def _dense_recall(
        session_factory: async_sessionmaker[AsyncSession],
        query: str,
        device_type: str,
        knowledge_version: str,
        embedding_backend: EmbeddingBackend,
        limit: int,
) -> list[ScoredHit]:
    """使用 pgvector cosine 距离召回；无向量的 chunk 不参与。"""
    query_vector = await embedding_backend.embed_query(query)
    if not math.isfinite(sum(abs(value) for value in query_vector)):
        raise ValueError("embedding query contains non-finite values")
    distance = KnowledgeChunk.embedding.cosine_distance(query_vector)
    async with session_factory() as session:
        result = await session.execute(
            select(KnowledgeChunk, KnowledgeSource, distance)
            .join(KnowledgeSource, KnowledgeChunk.source_id == KnowledgeSource.id)
            .where(
                *_base_filters(
                    KnowledgeChunk,
                    KnowledgeSource,
                    device_type,
                    knowledge_version,
                ),
                KnowledgeChunk.embedding.is_not(None),
            )
            .order_by(distance, KnowledgeChunk.id)
            .limit(limit)
        )
        rows: list[Any] = list(result.all())
        return [
            ScoredHit(
                hit=_hit(chunk, source, score=max(0.0, 1.0 - row_distance)),
                rank=rank,
                score=max(0.0, 1.0 - row_distance),
                channel="dense",
            )
            for rank, (chunk, source, row_distance) in enumerate(rows, start=1)
        ]


async def search_fault_knowledge_detail(
        query: str,
        device_type: str,
        *,
        symptom_keys: list[str] | None = None,
        cause_keys: list[str] | None = None,
        knowledge_version: str | None = None,
        top_k: int = 4,
        embedding_backend: EmbeddingBackend | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> tuple[RetrievalResult, KnowledgeRetrievalStats]:
    """执行三路召回、RRF 融合；通道失败时降级并记录 fallback 标签。"""
    if not query.strip():
        raise ValueError("knowledge query must not be empty")
    if not device_type.strip():
        raise ValueError("knowledge device_type must not be empty")
    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")
    version = knowledge_version or get_settings().knowledge_version
    if session_factory is None:
        from app.database import session_factory as default_session_factory

        session_factory = default_session_factory

    stats = KnowledgeRetrievalStats(query=query)
    recall_limit = min(RECALL_LIMIT, max(top_k, 20))
    channels: dict[str, list[ScoredHit]] = {}

    try:
        structured = await _structured_recall(
            session_factory,
            query,
            device_type,
            version,
            symptom_keys,
            cause_keys,
            recall_limit,
        )
        channels["structured"] = structured
        stats.structured_hits = len(structured)
    except Exception as exc:
        stats.fallbacks.append("structured_unavailable")
        logger.warning("structured knowledge retrieval failed", exc_info=exc)

    try:
        keyword = await _keyword_recall(
            session_factory,
            query,
            device_type,
            version,
            recall_limit,
        )
        channels["keyword"] = keyword
        stats.keyword_hits = len(keyword)
    except Exception as exc:
        stats.fallbacks.append("keyword_unavailable")
        logger.warning("keyword knowledge retrieval failed", exc_info=exc)

    if embedding_backend is None:
        stats.fallbacks.append("embedding_disabled")
    else:
        try:
            dense = await _dense_recall(
                session_factory,
                query,
                device_type,
                version,
                embedding_backend,
                recall_limit,
            )
            channels["dense"] = dense
            stats.dense_hits = len(dense)
        except Exception as exc:
            stats.fallbacks.append("dense_unavailable")
            logger.warning("dense knowledge retrieval failed", exc_info=exc)

    fused = fuse_candidates(channels, top_k=top_k)
    validate_citations([item.hit for item in fused])
    stats.final_hits = len(fused)
    result = RetrievalResult(
        query=query,
        hits=[item.hit for item in fused],
    )
    return result, stats


async def search_fault_knowledge(
        query: str,
        device_type: str,
        *,
        symptom_keys: list[str] | None = None,
        cause_keys: list[str] | None = None,
        knowledge_version: str | None = None,
        top_k: int = 4,
        embedding_backend: EmbeddingBackend | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> RetrievalResult:
    """三路混合检索入口；返回值与 citations 结构完全一致。"""
    result, _ = await search_fault_knowledge_detail(
        query=query,
        device_type=device_type,
        symptom_keys=symptom_keys,
        cause_keys=cause_keys,
        knowledge_version=knowledge_version,
        top_k=top_k,
        embedding_backend=embedding_backend,
        session_factory=session_factory,
    )
    return result
