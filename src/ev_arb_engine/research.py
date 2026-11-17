from datetime import datetime

from .arbitrage import ExecutionScenario, scan, simulate
from .models import Quote, require_compatible
from .pricing import consensus, conservative_ev


def analyze(quotes: list[Quote], at: datetime, budget: float = 100) -> dict:
    require_compatible(quotes)
    # One latest available snapshot per source; no look-ahead or stale reference prices.
    latest = {}
    for q in sorted(quotes, key=lambda q: (q.observed_at, q.received_at)):
        if q.available(at, 30):
            latest[q.source] = q
    fresh = list(latest.values())
    if len(fresh) < 3:
        raise ValueError("need three fresh sources for leave-one-source-out consensus")
    edges = []
    for q in fresh:
        fair = consensus(fresh, q.source)
        for outcome in q.outcomes:
            edges.append(
                {
                    "source": q.source,
                    "outcome": outcome.name,
                    "fair_probability": fair[outcome.name],
                    "conservative_ev": conservative_ev(outcome, fair[outcome.name]),
                }
            )
    plan = scan(fresh, at, budget)
    return {
        "as_of": at.isoformat(),
        "ev": edges,
        "arbitrage": plan.model_dump(mode="json") if plan else None,
        "stress": simulate(plan, at, fresh, ExecutionScenario(fill_fraction=0.6, adverse_move=0.03))
        if plan
        else None,
    }
