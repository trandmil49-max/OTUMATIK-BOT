"""
engines/rejection.py

Rejection Engine (SRS Part 14 SIGNAL REJECTION DATABASE: "Store every
rejected signal ... Every rejection must have a reason. Nothing should be
discarded without explanation."). The single place that turns a
`RejectionReason` produced by any upstream engine (RiskManagementEngine,
ConfidenceEngine, FastFilterEngine, a future duplicate-signal guard, ...)
into a persisted `Rejection` row via `RejectionRepository`.

Kept deliberately thin: this engine has no opinion about WHY something
was rejected, only about recording it completely and consistently. Every
other engine already knows its own `RejectionReason`; this one just owns
the one shared write path into the `rejections` table so that path is
never duplicated per-caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.models import Rejection, RejectionReason, SignalDirection
from infrastructure.database.repositories.rejection_repository import RejectionRepository


@dataclass(frozen=True)
class RejectionContext:
    """Optional scoring context to attach to a recorded rejection, gathered from whichever engines already ran."""

    direction: Optional[SignalDirection] = None
    confidence_score: Optional[float] = None
    risk_score: Optional[float] = None
    bitcoin_score: Optional[float] = None
    coin_trust_score: Optional[float] = None
    market_health_score: Optional[float] = None
    smart_money_score: Optional[float] = None
    secondary_reason: Optional[RejectionReason] = None
    rejected_filters: list[str] = field(default_factory=list)


class RejectionEngine:
    """Records rejected candidates and summarizes them for reporting -- the only writer of `Rejection` rows."""

    def __init__(self, repository: Optional[RejectionRepository] = None) -> None:
        self._repository = repository or RejectionRepository()

    def record(
        self,
        symbol: str,
        primary_reason: RejectionReason,
        context: Optional[RejectionContext] = None,
    ) -> Rejection:
        """Persist one rejection. `context` fields left unset are stored as NULL, never guessed."""
        ctx = context or RejectionContext()
        rejection = Rejection(
            symbol=symbol,
            primary_reason=primary_reason,
            direction=ctx.direction,
            confidence_score=ctx.confidence_score,
            risk_score=ctx.risk_score,
            bitcoin_score=ctx.bitcoin_score,
            coin_trust_score=ctx.coin_trust_score,
            market_health_score=ctx.market_health_score,
            smart_money_score=ctx.smart_money_score,
            secondary_reason=ctx.secondary_reason,
            rejected_filters=list(ctx.rejected_filters),
        )
        return self._repository.create(rejection)

    def get_reason_counts_since(self, since_iso: str) -> dict[str, int]:
        """Rejection-reason breakdown for a period -- feeds the future Daily Report's rejection summary (SRS Part 15)."""
        return self._repository.count_by_reason_since(since_iso)
