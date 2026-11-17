"""All provider data here are synthetic; no authenticated live requests."""

from dataclasses import replace

import pytest

from ev_arb_engine.provenance import AuditedCollector, EventMapping
from ev_arb_engine.research import analyze


class Feed:
    def __init__(self, quotes):
        self.quotes = quotes

    async def collect(self):
        return self.quotes


@pytest.mark.asyncio
async def test_duplicate_resellers_cannot_inflate_references(quotes, at):
    event = quotes[0].market.event
    mapping = EventMapping(event, event, "synthetic audit")
    original = quotes[1]
    reseller = original.model_copy(update={"source": "another-reseller"})
    adapter = AuditedCollector(
        Feed([*quotes, reseller]),
        {event.id: mapping},
        {**{q.source: q.source for q in quotes}, reseller.source: original.source},
        "synthetic",
    )
    normalized = await adapter.collect()
    assert analyze(normalized, at) == analyze(quotes, at)
    health = adapter.health(at, normalized)
    assert health["quote_count"] == 4
    assert health["provenance"]["another-reseller"] == original.source
    assert health["max_observed_age_seconds"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["home", "away", "starts_at", "sport", "missing_book"])
async def test_unverified_event_or_book_fails_closed(quotes, change):
    event = quotes[0].market.event
    expected = event
    if change == "starts_at":
        from datetime import timedelta

        expected = event.model_copy(update={change: event.starts_at + timedelta(seconds=1)})
    elif change != "missing_book":
        expected = event.model_copy(update={change: "unverified"})
    adapter = AuditedCollector(
        Feed(quotes),
        {event.id: EventMapping(expected, event, "synthetic audit")},
        {"unknown": "book"} if change == "missing_book" else {q.source: q.source for q in quotes},
        "synthetic",
    )
    with pytest.raises(ValueError, match="unknown or changed|unknown original"):
        await adapter.collect()


def test_audit_reference_required(quotes):
    mapping = EventMapping(quotes[0].market.event, quotes[0].market.event, "test")
    with pytest.raises(ValueError, match="audit"):
        replace(mapping, audit=" ")
