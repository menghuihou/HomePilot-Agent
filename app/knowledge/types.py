"""知识检索通道的共享值对象；融合纯函数见 app.knowledge.fusion。"""

from dataclasses import dataclass

from app.schemas import RetrievalHit


@dataclass(frozen=True, slots=True)
class ScoredHit:
    """单通道召回项；rank 从 1 开始，供 RRF 计算。"""

    hit: RetrievalHit
    rank: int
    score: float
    channel: str
