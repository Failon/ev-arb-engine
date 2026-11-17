"""Explicit, operator-audited identities for joining permitted provider feeds."""

from dataclasses import dataclass
from datetime import datetime

from .collectors import Collector
from .models import Event, Quote


@dataclass(frozen=True)
class EventMapping:
    external: Event
    canonical: Event
    audit: str

    def __post_init__(self):
        if not self.audit.strip():
            raise ValueError("event mapping requires an audit reference")


class AuditedCollector:
    """Fail closed before passing quotes into a multi-provider scanner.

    Book IDs identify the original bookmaker/region, not the reseller. Supply the
    same ID for every feed of that book; never invent an independent reference.
    """

    def __init__(
        self,
        collector: Collector,
        events: dict[str, EventMapping],
        books: dict[str, str],
        provider: str,
    ):
        if not provider or not books or any(not key.strip() for key in books.values()):
            raise ValueError("provider and audited bookmaker identities required")
        self.collector, self.events, self.books, self.provider = collector, events, books, provider
        self.last_health: dict = {}

    async def collect(self) -> list[Quote]:
        raw = await self.collector.collect()
        result = []
        for quote in raw:
            mapping = self.events.get(quote.market.event.id)
            if mapping is None or quote.market.event != mapping.external:
                raise ValueError("unknown or changed audited event mapping")
            if quote.source not in self.books:
                raise ValueError("unknown original bookmaker identity")
            market = quote.market.model_copy(update={"event": mapping.canonical})
            result.append(
                quote.model_copy(update={"market": market, "source": self.books[quote.source]})
            )
        self.last_health = {
            "provider": self.provider,
            "quote_count": len(result),
            "provenance": dict(self.books),
            "markets": sorted({q.market.kind for q in result}),
        }
        return result

    def health(self, at: datetime, quotes: list[Quote]) -> dict:
        ages = [(at - q.observed_at).total_seconds() for q in quotes]
        return {**self.last_health, "max_observed_age_seconds": max(ages, default=None)}
