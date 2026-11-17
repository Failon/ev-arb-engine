import math
from statistics import fmean

from .models import Outcome, Quote, require_compatible


def devig(odds: list[float], method: str = "multiplicative") -> list[float]:
    if len(odds) < 2 or any(not math.isfinite(x) or x <= 1 for x in odds):
        raise ValueError("finite decimal odds > 1 required")
    raw = [1 / x for x in odds]
    if method == "multiplicative":
        return [x / sum(raw) for x in raw]
    if method == "additive":
        p = [x - (sum(raw) - 1) / len(raw) for x in raw]
        if any(x <= 0 or x >= 1 for x in p):
            raise ValueError("additive devig produced invalid probabilities")
        return p
    if method == "power":
        lo, hi = 0.0, 1.0
        while sum(x**hi for x in raw) > 1:
            hi *= 2
        for _ in range(100):
            mid = (lo + hi) / 2
            if sum(x**mid for x in raw) > 1:
                lo = mid
            else:
                hi = mid
        return [x ** ((lo + hi) / 2) for x in raw]
    raise ValueError("unknown devig method")


def consensus(quotes: list[Quote], target_source: str, method="power") -> dict[str, float]:
    market = require_compatible(quotes)
    refs = [q for q in quotes if q.source != target_source]
    if len(refs) < 2 or len({q.source for q in refs}) != len(refs):
        raise ValueError("at least two distinct reference sources; one snapshot per source")
    probabilities = [devig([o.odds for o in q.outcomes], method) for q in refs]
    return {name: fmean(p[i] for p in probabilities) for i, name in enumerate(market.outcomes)}


def conservative_ev(
    outcome: Outcome, probability: float, haircut: float = 0.02, cost: float = 0.0
) -> float:
    if not all(math.isfinite(x) for x in (probability, haircut, cost)):
        raise ValueError("finite inputs required")
    if not 0 <= probability <= 1 or not 0 <= haircut <= 1 or cost < 0:
        raise ValueError("invalid probability, haircut or cost")
    return max(0, probability - haircut) * outcome.effective_odds - 1 - cost


def clv(taken: Outcome, closing_probability: float) -> float:
    """Fair-price CLV; positive means taken effective price beats closing fair price."""
    return conservative_ev(taken, closing_probability, haircut=0)
