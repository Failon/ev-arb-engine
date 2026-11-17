"""Quote-to-trade replay. One decision and at most one filled trade per event."""

import math
from datetime import datetime, timedelta

from pydantic import AwareDatetime, Field, model_validator

from .backtest import ResearchTrade, walk_forward
from .models import Market, Quote, StrictModel, require_compatible
from .pricing import clv, consensus, conservative_ev


class ReplayConfig(StrictModel):
    stake: float = Field(default=10, gt=0)
    min_ev: float = Field(default=0, ge=0)
    haircut: float = Field(default=0.02, ge=0, le=1)
    max_age: float = Field(default=30, ge=0)
    latency_ms: int = Field(default=500, ge=0)
    fill_fraction: float = Field(default=1, ge=0, le=1)
    adverse_move: float = Field(default=0, ge=0, lt=1)


class ReplayEvent(StrictModel):
    quotes: tuple[Quote, ...] = Field(min_length=1)
    decided_at: AwareDatetime
    closing_at: AwareDatetime
    settled_at: AwareDatetime
    winner: str | None  # None is an explicit void, not an unknown result

    @model_validator(mode="after")
    def validate_history(self):
        market = require_compatible(list(self.quotes))
        if not self.decided_at < self.closing_at < market.event.starts_at <= self.settled_at:
            raise ValueError("require decision < close < start <= settlement")
        if self.winner is not None and self.winner not in market.outcomes:
            raise ValueError("winner is not a market outcome")
        keys = [(q.source, q.received_at, q.observed_at) for q in self.quotes]
        if len(keys) != len(set(keys)):
            raise ValueError("ambiguous duplicate source snapshot")
        return self


class ReplayDataset(StrictModel):
    events: tuple[ReplayEvent, ...] = Field(min_length=1)
    boundaries: tuple[AwareDatetime, ...] = Field(min_length=2)
    config: ReplayConfig = ReplayConfig()

    @model_validator(mode="after")
    def unique_events(self):
        ids = [e.quotes[0].market.event.id for e in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("only one decision per event")
        if list(self.boundaries) != sorted(set(self.boundaries)):
            raise ValueError("boundaries must be strictly increasing")
        return self


def snapshot(quotes: tuple[Quote, ...], at: datetime, max_age: float) -> list[Quote]:
    """Newest source observation received by cutoff; delayed old messages cannot overwrite it."""
    latest = {}
    for quote in sorted(quotes, key=lambda q: (q.observed_at, q.received_at)):
        if quote.available(at, max_age):
            latest[quote.source] = quote
    return list(latest.values())


def replay_event(event: ReplayEvent, config: ReplayConfig) -> dict:
    market: Market = event.quotes[0].market
    result = {"event_id": market.event.id, "status": "no_signal", "trade": None}
    decision = snapshot(event.quotes, event.decided_at, config.max_age)
    if len(decision) < 3:
        return {**result, "status": "insufficient_references"}
    candidates = []
    for quote in decision:
        fair = consensus(decision, quote.source)
        for outcome in quote.outcomes:
            ev = conservative_ev(outcome, fair[outcome.name], config.haircut)
            if ev >= config.min_ev and outcome.liquidity > 0:
                candidates.append((ev, quote.source, outcome.name, outcome))
    if not candidates:
        return result
    ev, source, name, offered = max(candidates, key=lambda c: (c[0], c[1], c[2]))
    result.update(source=source, outcome=name, predicted_ev=ev, offered_odds=offered.odds)
    arrival = event.decided_at + timedelta(milliseconds=config.latency_ms)
    arrived = next(
        (q for q in snapshot(event.quotes, arrival, config.max_age) if q.source == source), None
    )
    if arrived is None:
        return {**result, "status": "unfilled"}
    available = next(o for o in arrived.outcomes if o.name == name)
    stake = math.floor(min(config.stake * config.fill_fraction, available.liquidity) * 100) / 100
    if stake <= 0:
        return {**result, "status": "unfilled"}
    taken = available.model_copy(
        update={"odds": 1 + (available.odds - 1) * (1 - config.adverse_move)}
    )
    # Result/close are first used AFTER selection and execution; never influence the signal.
    net = 0 if event.winner is None else taken.effective_odds - 1 if name == event.winner else -1
    trade = ResearchTrade(
        event_id=market.event.id,
        decided_at=event.decided_at,
        settled_at=event.settled_at,
        predicted_ev=ev,
        net_return=net,
    )
    closing = snapshot(event.quotes, event.closing_at, config.max_age)
    references = [q for q in closing if q.source != source]
    closing_clv = clv(taken, consensus(references, source)[name]) if len(references) >= 2 else None
    return {
        **result,
        "status": "filled",
        "arrival": arrival.isoformat(),
        "stake": stake,
        "taken_odds": taken.odds,
        "effective_odds": taken.effective_odds,
        "profit": stake * net,
        "clv": closing_clv,
        "clv_status": "available" if closing_clv is not None else "missing_references",
        "trade": trade.model_dump(mode="json"),
    }


def replay(dataset: ReplayDataset) -> dict:
    records = [replay_event(event, dataset.config) for event in dataset.events]
    trades = [ResearchTrade.model_validate(r["trade"]) for r in records if r["trade"]]
    return {
        "records": records,
        "walk_forward": walk_forward(trades, list(dataset.boundaries)),
        "filled": len(trades),
        "profit": sum(r.get("profit", 0) for r in records),
        "note": "walk_forward uses unit returns; profit uses simulated filled stakes",
    }
