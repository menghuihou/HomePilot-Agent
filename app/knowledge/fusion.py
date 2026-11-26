"""知识检索融合与 citation 安全校验的纯函数层。"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.knowledge.types import ScoredHit
from app.schemas import RetrievalHit


@dataclass(frozen=True, slots=True)
class FusedKnowledgeHit:
    """RRF 原始分与已回写 normalized score 的最终 hit。"""

    hit: RetrievalHit
    score: float


class CitationValidationError(ValueError):
    """citation 必填字段、score 或设备域非法；阻断结果进入诊断状态。"""


def _identity(hit: RetrievalHit) -> tuple[int, str]:
    """跨通道去重键：source + chunk key。"""
    return hit.citation.source_id, str(hit.metadata.get("chunk_key"))


def deduplicate_hits(candidates: Sequence[ScoredHit]) -> list[ScoredHit]:
    """按 source + chunk key 去重；同 key 保留分数最高候选。"""
    best: dict[tuple[int, str], ScoredHit] = {}
    for candidate in candidates:
        key = _identity(candidate.hit)
        previous = best.get(key)
        if previous is None or _candidate_key(candidate) < _candidate_key(previous):
            best[key] = candidate
    return sorted(best.values(), key=_candidate_key)


def _candidate_key(candidate: ScoredHit) -> tuple[float, int, int, str]:
    """分数高、rank 小、chunk id 小者优先，最后用 channel 稳定排序。"""
    return (
        -candidate.score,
        candidate.rank,
        candidate.hit.citation.chunk_id,
        candidate.channel,
    )


def fuse_candidates(
    channels: Mapping[str, Sequence[ScoredHit]],
    *,
    k: int = 60,
    top_k: int = 4,
) -> list[FusedKnowledgeHit]:
    """按 Reciprocal Rank Fusion 聚合多通道候选。"""
    if k <= 0:
        raise ValueError("RRF k must be positive")
    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")

    grouped: dict[tuple[int, str], list[ScoredHit]] = {}
    for _channel, candidates in channels.items():
        for candidate in candidates:
            grouped.setdefault(_identity(candidate.hit), []).append(candidate)
    active_channels = {
        candidate.channel for candidates in grouped.values() for candidate in candidates
    }
    if not active_channels:
        return []

    maximum = len(active_channels) / (k + 1)
    fused: list[FusedKnowledgeHit] = []
    for candidates in grouped.values():
        raw_score = sum(1 / (k + candidate.rank) for candidate in candidates)
        normalized = min(1.0, raw_score / maximum)
        representative = deduplicate_hits(candidates)[0]
        citation = representative.hit.citation.model_copy(update={"score": normalized})
        fused.append(
            FusedKnowledgeHit(
                hit=representative.hit.model_copy(update={"citation": citation}),
                score=raw_score,
            )
        )

    fused.sort(
        key=lambda item: (
            -item.score,
            -item.hit.citation.score,
            item.hit.citation.chunk_id,
        )
    )
    return fused[:top_k]


def validate_citations(hits: Sequence[RetrievalHit]) -> None:
    """验证 citation 必填字段、score 和 chunk key；失败必须阻断报告引用。"""
    if not hits:
        return
    expected_domain = hits[0].citation.device_type
    expected_version = hits[0].citation.knowledge_version
    for hit in hits:
        citation = hit.citation
        required = (
            citation.source_id,
            citation.chunk_id,
            citation.source_key,
            citation.title,
            citation.chunk_index,
            citation.device_type,
            citation.knowledge_version,
        )
        if any(value is None or value == "" for value in required):
            raise CitationValidationError("citation has a missing required field")
        if not isinstance(hit.metadata.get("chunk_key"), str) or not hit.metadata.get(
            "chunk_key"
        ):
            raise CitationValidationError("citation is missing chunk_key")
        if not math.isfinite(citation.score) or not 0 <= citation.score <= 1:
            raise CitationValidationError("citation score is outside [0, 1]")
        if citation.device_type != expected_domain:
            raise CitationValidationError("citation device domains are inconsistent")
        if citation.knowledge_version != expected_version:
            raise CitationValidationError(
                "citation knowledge versions are inconsistent"
            )
    assert_same_device_domain(hits, expected_domain)


def assert_same_device_domain(
    hits: Sequence[RetrievalHit], expected_device_type: str | None = None
) -> None:
    """任何跨设备域 citation 都是权限 / 召回边界错误。"""
    if not hits:
        return
    domains = {hit.citation.device_type for hit in hits}
    if len(domains) > 1:
        raise CitationValidationError(
            f"cross-domain citations found: {sorted(domains)}"
        )
    domain = next(iter(domains))
    if expected_device_type is not None and domain != expected_device_type:
        raise CitationValidationError(
            f"citation domain {domain} does not match query domain {expected_device_type}"
        )
