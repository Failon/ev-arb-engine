"""Bounded autonomous research scans; no network source is enabled implicitly."""

import asyncio
import hashlib
import json
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import AwareDatetime, Field

from .arbitrage import scan
from .collectors import Collector, ProviderRateLimit
from .models import Quote, StrictModel
from .research import analyze


class Signal(StrictModel):
    kind: str
    fingerprint: str
    decided_at: AwareDatetime
    quotes: tuple[Quote, ...]
    detail: dict
    synthetic: bool


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class ScanConfig(StrictModel):
    max_age: float = Field(default=30, ge=0, le=30)
    budget: float = Field(default=100, gt=0)
    min_ev: float = Field(default=0, ge=0)
    min_arb_profit: float = Field(default=0, ge=0)
    sports: tuple[str, ...] = ()
    markets: tuple[str, ...] = ("moneyline", "total", "spread")


@dataclass
class FeedSlot:
    name: str
    collector: Collector
    requests_per_hour: int = 60
    attempts: deque = field(default_factory=deque)
    blocked_until: datetime | None = None
    failures: int = 0

    def __post_init__(self):
        if not self.name or not 1 <= self.requests_per_hour <= 5000:
            raise ValueError("feed name and explicit hourly quota required")


class Scanner:
    def __init__(self, feeds: list[FeedSlot], config: ScanConfig, *, synthetic: bool = True):
        if not feeds or len({f.name for f in feeds}) != len(feeds):
            raise ValueError("distinct feeds required")
        # Production wiring must use audited provenance wrappers; CLI is synthetic only.
        if not synthetic:
            from .provenance import AuditedCollector

            if any(not isinstance(f.collector, AuditedCollector) for f in feeds):
                raise ValueError("real feeds require audited collector wrappers")
        self.feeds, self.config, self.synthetic = feeds, config, synthetic
        self.last_at: datetime | None = None

    async def tick(self, at: datetime | None = None) -> dict:
        wall_clock = at is None
        at = at or datetime.now(UTC)
        if at.tzinfo is None or (self.last_at is not None and at < self.last_at):
            raise ValueError("aware nondecreasing scan time required")
        self.last_at = at

        async def collect(slot):
            while slot.attempts and slot.attempts[0] <= at - timedelta(hours=1):
                slot.attempts.popleft()
            if (slot.blocked_until and at < slot.blocked_until) or len(
                slot.attempts
            ) >= slot.requests_per_hour:
                return [], {"feed": slot.name, "status": "cooldown_or_quota"}
            slot.attempts.append(at)
            try:
                quotes = await slot.collector.collect()
            except ProviderRateLimit as exc:
                slot.blocked_until = at + timedelta(seconds=exc.retry_after)
                return [], {"feed": slot.name, "status": "rate_limited"}
            except Exception:  # noqa: BLE001 - isolate each provider boundary
                # Never expose exception strings: provider errors can contain credentials.
                slot.failures += 1
                delay = min(3600, 60 * 2 ** min(slot.failures - 1, 6))
                slot.blocked_until = at + timedelta(seconds=delay)
                return [], {"feed": slot.name, "status": "unavailable", "backoff_seconds": delay}
            slot.failures = 0
            cutoff = datetime.now(UTC) if wall_clock else at
            fresh = [q for q in quotes if q.available(cutoff, self.config.max_age)]
            return fresh, {
                "feed": slot.name,
                "status": "ok",
                "quote_count": len(quotes),
                "fresh_count": len(fresh),
                "max_age_seconds": max(
                    ((cutoff - q.observed_at).total_seconds() for q in fresh), default=None
                ),
                "provenance": getattr(slot.collector, "last_health", {}),
            }

        batches = await asyncio.gather(*(collect(f) for f in self.feeds))
        if wall_clock:
            at = datetime.now(UTC)
            self.last_at = at
        groups = defaultdict(list)
        for quotes, _ in batches:
            for quote in quotes:
                market = quote.market
                if self.config.sports and market.event.sport not in self.config.sports:
                    continue
                if market.kind not in self.config.markets:
                    continue
                groups[market.model_dump_json()].append(quote)
        signals, rejected = [], 0
        for key in sorted(groups):
            quotes = groups[key]
            latest = {}
            for quote in sorted(quotes, key=lambda q: (q.observed_at, q.received_at, q.source)):
                latest[quote.source] = quote
            snapshots = [latest[source] for source in sorted(latest)]
            try:
                result = analyze(snapshots, at, self.config.budget)
            except ValueError:
                rejected += 1
                plan = scan(snapshots, at, self.config.budget, self.config.max_age)
                result = {"ev": [], "arbitrage": plan.model_dump(mode="json") if plan else None}
            for edge in result["ev"]:
                if edge["conservative_ev"] <= self.config.min_ev:
                    continue
                # Include all references in the fingerprint, not only the target price.
                signals.append(self._signal("ev", snapshots, edge, at))
            if (
                result["arbitrage"]
                and result["arbitrage"]["worst_profit"] > self.config.min_arb_profit
            ):
                signals.append(self._signal("arbitrage", snapshots, result["arbitrage"], at))
        return {"signals": signals, "health": [h for _, h in batches], "rejected_groups": rejected}

    def _signal(self, kind, snapshots, detail, at):
        digest = fingerprint(
            {"kind": kind, "quotes": [q.model_dump(mode="json") for q in snapshots]}
        )
        # Each selection has its own identity even when it shares a market snapshot.
        digest = fingerprint(
            {"snapshot": digest, "source": detail.get("source"), "outcome": detail.get("outcome")}
        )
        return Signal(
            kind=kind,
            fingerprint=digest,
            decided_at=at,
            quotes=tuple(snapshots),
            detail=detail,
            synthetic=self.synthetic,
        )

    async def run(self, *, iterations: int, interval: float = 60, on_batch=None):
        if isinstance(iterations, bool) or not 1 <= iterations <= 10000:
            raise ValueError("bounded iteration count required")
        if not math.isfinite(interval) or interval < 1:
            raise ValueError("scan interval must be at least one second")
        for index in range(iterations):
            batch = await self.tick()
            if on_batch:
                await on_batch(batch)
            if index + 1 < iterations:
                await asyncio.sleep(interval)
