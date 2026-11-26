"""摄取故障知识 chunk 到 knowledge_sources / knowledge_chunks。

用法：

    uv run python -m db.seeds.embed_fault_knowledge

幂等口径：当前版本内按 source 全量重放；hash 相同的 active chunk 保留，
hash 变化或来源中删除的 chunk 置为 retired，新 chunk 另插一行，保留历史。
EMBEDDING_ENABLED=false 时只写关键词与结构化索引字段；true 时按批生成向量。
"""

import asyncio
import logging

import httpx
from sqlalchemy import func, insert, select

from app.config import get_settings
from app.database import session_factory
from app.knowledge.chunker import (
    KnowledgeChunkSeed,
    generate_domain_chunks,
    load_fault_knowledge_domains,
)
from app.knowledge.embedding import EmbeddingBackend, OpenAIEmbeddingBackend
from app.models import KnowledgeChunk, KnowledgeSource

logger = logging.getLogger(__name__)
EMBED_BATCH_SIZE = 32


async def _embed_items(
    items: list[tuple[int, KnowledgeChunkSeed]],
    embedding_backend: EmbeddingBackend,
) -> tuple[dict[int, list[float]], int]:
    """批量生成缺失向量；一批失败时保留 chunk，仅计入失败数量。"""
    if not items:
        return {}, 0

    embeddings: dict[int, list[float]] = {}
    embedded_count = 0
    for start in range(0, len(items), EMBED_BATCH_SIZE):
        batch = items[start : start + EMBED_BATCH_SIZE]
        try:
            vectors = await embedding_backend.embed_documents(
                [chunk.chunk_text for _, chunk in batch]
            )
        except Exception as exc:
            logger.warning(
                "embedding batch failed: batch_start=%d size=%d error=%s",
                start,
                len(batch),
                type(exc).__name__,
            )
            continue
        embedded_count += len(batch)
        for (item_id, _), vector in zip(batch, vectors, strict=True):
            embeddings[item_id] = vector
    return embeddings, embedded_count


async def _seed(knowledge_version: str) -> None:
    """执行当前版本全量重放。"""
    settings = get_settings()
    embedding_client: httpx.AsyncClient | None = None
    embedding_backend: EmbeddingBackend | None = None
    if settings.embedding_enabled:
        # 持久 client：多批次共用 HTTP 连接池，避免每批重建连接。
        embedding_client = httpx.AsyncClient(timeout=settings.embedding_timeout_seconds)
        embedding_backend = OpenAIEmbeddingBackend(settings, client=embedding_client)
    domains = load_fault_knowledge_domains()
    source_inserted = 0
    source_updated = 0
    chunk_inserted = 0
    chunk_replaced = 0
    chunk_retained = 0
    chunk_retired = 0
    embedding_failed = 0
    embeddings_succeeded = 0

    async with session_factory() as session:
        for domain in domains:
            chunks = generate_domain_chunks(domain, knowledge_version)
            source = await session.scalar(
                select(KnowledgeSource).where(
                    KnowledgeSource.source_key == domain.source_key,
                    KnowledgeSource.knowledge_version == knowledge_version,
                )
            )
            if source is None:
                source = KnowledgeSource(
                    source_key=domain.source_key,
                    title=domain.source_title,
                    device_type=domain.device_type,
                    knowledge_version=knowledge_version,
                    synthetic=True,
                    source_type="seed",
                )
                session.add(source)
                await session.flush()
                source_inserted += 1
            elif (
                source.title != domain.source_title
                or source.device_type != domain.device_type
            ):
                source.title = domain.source_title
                source.device_type = domain.device_type
                source_updated += 1

            active_rows = await session.execute(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.source_id == source.id,
                    KnowledgeChunk.knowledge_version == knowledge_version,
                    KnowledgeChunk.status == "active",
                )
            )
            active_by_key = {row.chunk_key: row for row in active_rows.scalars()}
            desired_by_key = {chunk.chunk_key: chunk for chunk in chunks}

            missing_embedding: list[tuple[int, KnowledgeChunk, KnowledgeChunkSeed]] = []
            new_items: list[tuple[KnowledgeChunkSeed, list[float] | None]] = []
            pending_embedding: list[tuple[int, KnowledgeChunkSeed]] = []
            for chunk in chunks:
                active = active_by_key.get(chunk.chunk_key)
                if active is not None and active.chunk_hash == chunk.chunk_hash:
                    chunk_retained += 1
                    if active.embedding is None and settings.embedding_enabled:
                        missing_embedding.append((active.id, active, chunk))
                    continue
                if active is not None:
                    active.status = "retired"
                    chunk_replaced += 1
                else:
                    chunk_inserted += 1
                pending_embedding.append((len(pending_embedding), chunk))

            for key, active in active_by_key.items():
                if key not in desired_by_key:
                    active.status = "retired"
                    chunk_retired += 1

            await session.flush()
            if settings.embedding_enabled:
                new_embeddings, embedded_count = await _embed_items(
                    pending_embedding, embedding_backend
                )
                embeddings_succeeded += embedded_count
                embedding_failed += len(pending_embedding) - embedded_count
                new_items = [
                    (chunk, new_embeddings.get(item_id))
                    for item_id, chunk in pending_embedding
                ]
                retained_targets = [
                    (item_id, chunk) for item_id, _, chunk in missing_embedding
                ]
                retained_embeddings, embedded_count = await _embed_items(
                    retained_targets, embedding_backend
                )
                embeddings_succeeded += embedded_count
                embedding_failed += len(retained_targets) - embedded_count
                for item_id, item, _chunk in missing_embedding:
                    vector = retained_embeddings.get(item_id)
                    if vector is not None:
                        item.embedding = vector
            else:
                new_items = [(chunk, None) for _, chunk in pending_embedding]

            for chunk, embedding in new_items:
                await session.execute(
                    insert(KnowledgeChunk).values(
                        source_id=source.id,
                        knowledge_version=knowledge_version,
                        chunk_key=chunk.chunk_key,
                        title=chunk.title,
                        chunk_text=chunk.chunk_text,
                        search_text=chunk.search_text,
                        search_tsv=func.to_tsvector("simple", chunk.search_text),
                        embedding=embedding,
                        metadata=chunk.metadata,
                        chunk_hash=chunk.chunk_hash,
                        status="active",
                    )
                )

        await session.commit()

    if embedding_client is not None:
        await embedding_client.aclose()

    print(
        f"knowledge seed version={knowledge_version} "
        f"sources_inserted={source_inserted} sources_updated={source_updated} "
        f"chunks_inserted={chunk_inserted} chunks_replaced={chunk_replaced} "
        f"chunks_retained={chunk_retained} chunks_retired={chunk_retired} "
        f"embeddings_succeeded={embeddings_succeeded if settings.embedding_enabled else 0} "
        f"embeddings_failed={embedding_failed if settings.embedding_enabled else 0}"
    )


def main() -> None:
    """CLI 入口；知识版本统一来自配置。"""
    asyncio.run(_seed(get_settings().knowledge_version))


if __name__ == "__main__":
    main()
