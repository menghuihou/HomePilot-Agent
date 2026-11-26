"""知识混合检索测试：RRF 纯函数、citation 红线和真实三路召回。"""

import asyncio
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.knowledge.embedding import (
    EmbeddingAPIError,
    FakeEmbeddingBackend,
    _parse_embedding_response,
)
from app.knowledge.fusion import (
    CitationValidationError,
    assert_same_device_domain,
    deduplicate_hits,
    fuse_candidates,
    validate_citations,
)
from app.knowledge.repository import (
    ScoredHit,
    search_fault_knowledge,
    search_fault_knowledge_detail,
)
from app.models import KnowledgeChunk
from app.schemas import Citation, RetrievalHit


def _citation(
    chunk_id: int,
    *,
    source_id: int = 1,
    source_key: str = "lighting_knowledge",
    device_type: str = "light",
) -> Citation:
    return Citation(
        source_id=source_id,
        chunk_id=chunk_id,
        source_key=source_key,
        title=f"cause {chunk_id}",
        chunk_index=chunk_id,
        device_type=device_type,
        knowledge_version=get_settings().knowledge_version,
        score=0.1,
    )


def _hit(chunk_id: int, **citation_values: Any) -> RetrievalHit:
    citation_values.pop("chunk_id", None)
    return RetrievalHit(
        citation=_citation(chunk_id, **citation_values),
        chunk_text=f"故障说明 {chunk_id}",
        metadata={"chunk_key": f"cause:{chunk_id}"},
    )


def _candidate(
    hit: RetrievalHit,
    rank: int,
    score: float,
    channel: str,
) -> ScoredHit:
    return ScoredHit(hit=hit, rank=rank, score=score, channel=channel)


def _run_database_search(operation):
    """每个检索测试用独立引擎，避免连接池跨 asyncio 事件循环复用。"""
    engine = create_async_engine(get_settings().database_url)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def run():
        try:
            return await operation(session_maker)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_rrf_promotes_hit_found_by_multiple_channels():
    shared = _hit(12)
    fused = fuse_candidates(
        {
            "structured": [_candidate(shared, 1, 0.9, "structured")],
            "keyword": [_candidate(shared, 2, 0.5, "keyword")],
            "dense": [_candidate(_hit(99), 1, 0.7, "dense")],
        },
        top_k=2,
    )

    assert len(fused) == 2
    assert fused[0].hit.citation.chunk_id == 12
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert 0 < fused[0].hit.citation.score <= 1


def test_deduplicate_hits_keeps_best_score_per_source_chunk_key():
    best = _hit(12)
    candidates = [
        _candidate(_hit(12), 2, 0.8, "keyword"),
        _candidate(best, 1, 0.9, "structured"),
        _candidate(_hit(20), 1, 0.7, "dense"),
    ]

    deduplicated = deduplicate_hits(candidates)

    assert [candidate.hit.citation.chunk_id for candidate in deduplicated] == [12, 20]


def test_validate_citations_rejects_missing_chunk_key_and_cross_domain():
    valid = _hit(1)
    validate_citations([valid])

    missing_key = RetrievalHit(
        citation=valid.citation,
        chunk_text=valid.chunk_text,
        metadata={},
    )
    with pytest.raises(CitationValidationError, match="missing chunk_key"):
        validate_citations([missing_key])

    cross_domain = _hit(2, device_type="ac")
    with pytest.raises(
        CitationValidationError, match="citation device domains are inconsistent"
    ):
        validate_citations([valid, cross_domain])


def test_assert_same_device_domain_rejects_query_domain_mismatch():
    with pytest.raises(CitationValidationError, match="does not match query domain"):
        assert_same_device_domain([_hit(1)], expected_device_type="ac")


def test_fake_embedding_is_deterministic_and_normalized():
    backend = FakeEmbeddingBackend(dimension=32)

    first = asyncio.run(backend.embed_query("灯不亮 reset breaker"))
    second = asyncio.run(backend.embed_query("灯不亮 reset breaker"))
    documents = asyncio.run(
        backend.embed_documents(["灯不亮 reset breaker", "空调不制冷"])
    )

    assert first == second
    assert first == documents[0]
    assert len(first) == len(documents[1]) == 32
    assert pytest.approx(sum(value * value for value in first)) == 1.0


def test_http_embedding_response_requires_complete_dimension_vectors():
    payload = {
        "data": [
            {"index": 0, "embedding": [1, 0]},
            {"index": 1, "embedding": [0, 1]},
        ]
    }

    assert _parse_embedding_response(payload, ["a", "b"], 2) == [[1.0, 0.0], [0.0, 1.0]]
    with pytest.raises(EmbeddingAPIError, match="dimension mismatch"):
        _parse_embedding_response(payload, ["a", "b"], 3)
    with pytest.raises(EmbeddingAPIError, match="count does not match"):
        _parse_embedding_response({"data": payload["data"][:1]}, ["a", "b"], 2)


class FailingEmbeddingBackend(FakeEmbeddingBackend):
    """模拟真实 embedding 服务不可用，验证 dense 通道可降级。"""

    async def embed_query(self, text: str) -> list[float]:
        raise EmbeddingAPIError("service unavailable")


def test_keyword_retrieval_hits_exact_action(db_ready):
    async def operation(session_maker):
        return await search_fault_knowledge(
            "reset breaker", "light", session_factory=session_maker
        )

    result = _run_database_search(operation)

    assert result.hits
    assert result.citations
    assert all(hit.citation.device_type == "light" for hit in result.hits)
    assert any(
        hit.metadata.get("cause_key") == "breaker_tripped" for hit in result.hits
    )


def test_structured_retrieval_matches_symptom_key(db_ready):
    async def operation(session_maker):
        return await search_fault_knowledge(
            "light_flicker",
            "light",
            symptom_keys=["light_flicker"],
            top_k=4,
            session_factory=session_maker,
        )

    result = _run_database_search(operation)

    assert 1 <= len(result.hits) <= 4
    assert result.citations
    assert all(hit.citation.device_type == "light" for hit in result.hits)
    assert all("light_flicker" in hit.metadata["symptom_keys"] for hit in result.hits)


def test_dense_retrieval_uses_temporary_test_vectors(db_ready):
    backend = FakeEmbeddingBackend(dimension=1024)

    async def operation(session_maker):
        async with session_maker() as session:
            source = await session.execute(
                select(
                    KnowledgeChunk.id,
                    KnowledgeChunk.chunk_text,
                ).where(
                    KnowledgeChunk.status == "active",
                    KnowledgeChunk.chunk_key == "cause:bulb_burned",
                )
            )
            row = source.first()
            if row is None:
                return None, None
            chunk_id, chunk_text = row
            vectors = await backend.embed_documents([chunk_text])
            await session.execute(
                update(KnowledgeChunk)
                .where(KnowledgeChunk.id == chunk_id)
                .values(embedding=vectors[0])
            )
            await session.commit()
        try:
            return chunk_id, await search_fault_knowledge(
                chunk_text,
                "light",
                embedding_backend=backend,
                top_k=3,
                session_factory=session_maker,
            )
        finally:
            async with session_maker() as session:
                await session.execute(
                    update(KnowledgeChunk)
                    .where(KnowledgeChunk.id == chunk_id)
                    .values(embedding=None)
                )
                await session.commit()

    chunk_id, result = _run_database_search(operation)

    assert chunk_id is not None
    assert result is not None
    assert result.hits
    assert result.hits[0].citation.chunk_id == chunk_id


def test_cross_domain_and_unknown_domain_queries_are_isolated(db_ready):
    query = "灯不亮"

    async def operation(session_maker):
        return (
            await search_fault_knowledge(
                query, "ac", top_k=4, session_factory=session_maker
            ),
            await search_fault_knowledge(
                query, "unknown_domain", top_k=4, session_factory=session_maker
            ),
        )

    other_domain, unknown_domain = _run_database_search(operation)

    assert all(hit.citation.device_type != "light" for hit in other_domain.hits)
    assert unknown_domain.hits == []
    assert unknown_domain.citations == []


def test_dense_failure_degrades_to_keyword_and_returns_result(db_ready):
    async def operation(session_maker):
        return await search_fault_knowledge_detail(
            "reset breaker",
            "light",
            embedding_backend=FailingEmbeddingBackend(),
            session_factory=session_maker,
        )

    result, stats = _run_database_search(operation)

    assert result.hits
    assert stats.dense_hits == 0
    assert "dense_unavailable" in stats.fallbacks
    assert stats.keyword_hits > 0
    assert stats.structured_hits >= 0
