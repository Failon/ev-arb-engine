from datetime import datetime
from itertools import pairwise

from pydantic import AwareDatetime, Field, model_validator

from .models import StrictModel


class ResearchTrade(StrictModel):
    event_id: str
    decided_at: AwareDatetime
    settled_at: AwareDatetime
    predicted_ev: float
    net_return: float = Field(ge=-1)  # realized per unit stake, including costs

    @model_validator(mode="after")
    def chronology(self):
        if self.settled_at <= self.decided_at:
            raise ValueError("settlement must follow decision")
        return self


def walk_forward(
    trades: list[ResearchTrade],
    boundaries: list[datetime],
    thresholds: tuple[float, ...] = (0, 0.02, 0.05),
    min_train: int = 3,
) -> list[dict]:
    """Expanding training set, purged by settlement and event; half-open test folds."""
    if len(boundaries) < 2 or any(t.tzinfo is None for t in boundaries):
        raise ValueError("at least two aware boundaries required")
    if boundaries != sorted(set(boundaries)) or not thresholds or min_train < 1:
        raise ValueError("invalid walk-forward configuration")
    if len({t.event_id for t in trades}) != len(trades):
        raise ValueError("MVP requires one research trade per event to prevent event leakage")
    folds = []
    for start, end in pairwise(boundaries):
        train = [t for t in trades if t.decided_at < start and t.settled_at < start]
        if len(train) < min_train:
            folds.append({"start": start.isoformat(), "status": "insufficient_training"})
            continue

        def score(threshold, train=train):
            chosen = [t.net_return for t in train if t.predicted_ev >= threshold]
            return sum(chosen) / len(chosen) if chosen else float("-inf")

        threshold = max(sorted(thresholds, reverse=True), key=score)
        test = [t for t in trades if start <= t.decided_at < end and t.predicted_ev >= threshold]
        # Report only resolved outcomes at fold end. Unresolved trades are explicit.
        resolved = [t for t in test if t.settled_at <= end]
        folds.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "status": "ok",
                "train_count": len(train),
                "threshold": threshold,
                "test_count": len(test),
                "unresolved": len(test) - len(resolved),
                "profit_units": sum(t.net_return for t in resolved),
                "roi": sum(t.net_return for t in resolved) / len(resolved) if resolved else None,
            }
        )
    return folds
