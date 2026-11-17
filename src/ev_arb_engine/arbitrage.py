import math
from datetime import datetime, timedelta

from pydantic import Field

from .models import Outcome, Quote, StrictModel, require_compatible


class Leg(StrictModel):
    quote: Quote
    outcome: Outcome
    stake: float = Field(gt=0)


class Arbitrage(StrictModel):
    legs: tuple[Leg, ...]
    total_stake: float
    worst_profit: float


def scan(
    quotes: list[Quote], at: datetime, budget: float = 100, max_age: float = 30, cost: float = 0
) -> Arbitrage | None:
    market = require_compatible(quotes)
    if not all(math.isfinite(v) for v in (budget, max_age, cost)):
        raise ValueError("finite parameters required")
    if budget <= 0 or max_age < 0 or cost < 0:
        raise ValueError("invalid budget, age or fixed cost")
    latest = {}
    for q in sorted(quotes, key=lambda q: (q.observed_at, q.received_at)):
        if q.available(at, max_age):
            latest[q.source] = q
    fresh = list(latest.values())
    if not fresh:
        return None
    selected = [
        max(
            ((q, q.outcomes[i]) for q in fresh if q.outcomes[i].liquidity > 0),
            key=lambda pair: pair[1].effective_odds,
            default=None,
        )
        for i in range(len(market.outcomes))
    ]
    if any(pair is None for pair in selected):
        return None
    inverse = sum(1 / o.effective_odds for _, o in selected)
    if inverse >= 1:
        return None
    payout = min(budget / inverse, *(o.liquidity * o.effective_odds for _, o in selected))
    # Round down stakes to cents, then recompute the guarantee (never claim idealized profit).
    legs = tuple(
        Leg(quote=q, outcome=o, stake=math.floor(payout / o.effective_odds * 100) / 100)
        for q, o in selected
        if math.floor(payout / o.effective_odds * 100) > 0
    )
    if len(legs) != len(market.outcomes):
        return None
    total = sum(leg.stake for leg in legs)
    worst = min(leg.stake * leg.outcome.effective_odds for leg in legs) - total - cost
    return Arbitrage(legs=legs, total_stake=total, worst_profit=worst) if worst > 0 else None


class ExecutionScenario(StrictModel):
    latency_ms: int = Field(default=500, ge=0)
    fill_fraction: float = Field(default=1, ge=0, le=1)
    adverse_move: float = Field(default=0, ge=0, lt=1)
    fixed_cost: float = Field(default=0, ge=0)
    max_age: float = Field(default=30, ge=0)


def simulate(
    plan: Arbitrage, at: datetime, snapshots: list[Quote], scenario: ExecutionScenario
) -> dict:
    """Deterministic stress replay. Snapshots are market state visible at arrival."""
    require_compatible([leg.quote for leg in plan.legs] + snapshots)
    arrival = at + timedelta(milliseconds=scenario.latency_ms)
    fills = []
    for leg in plan.legs:
        candidates = [
            q
            for q in snapshots
            if q.source == leg.quote.source and q.available(arrival, scenario.max_age)
        ]
        latest = max(candidates, key=lambda q: (q.observed_at, q.received_at), default=None)
        outcome = (
            next((o for o in latest.outcomes if o.name == leg.outcome.name), None)
            if latest
            else None
        )
        stake = min(leg.stake * scenario.fill_fraction, outcome.liquidity) if outcome else 0
        effective = 1 + (outcome.effective_odds - 1) * (1 - scenario.adverse_move) if outcome else 1
        fills.append({"outcome": leg.outcome.name, "stake": stake, "effective_odds": effective})
    spent = sum(f["stake"] for f in fills)
    pnl = {
        f["outcome"]: f["stake"] * f["effective_odds"] - spent - scenario.fixed_cost for f in fills
    }
    return {
        "arrival": arrival.isoformat(),
        "fills": fills,
        "pnl_by_outcome": pnl,
        "worst_profit": min(pnl.values()),
        "total_stake": spent,
    }
