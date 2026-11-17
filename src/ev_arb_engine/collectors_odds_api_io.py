"""Read-only Odds-API.io V3 adapter for audited pre-match moneyline quotes.

Docs: https://feedback.odds-api.io/en/help/articles/6035212-understanding-the-api-workflow
No live calls are made unless an application explicitly instantiates and invokes it.
"""

import json
import re
from datetime import UTC, datetime

import httpx

from .journal import Journal
from .models import Event, Market, Outcome, Quote, SettlementRules


class OddsAPIIOCollector:
    """Collect one audited V3 event. No implicit event matching or real bet placement."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        event_id: int,
        bookmakers: tuple[str, ...],
        rules: dict[str, SettlementRules],
        journal: Journal,
    ):
        if not api_key or not api_key.strip():
            raise ValueError("missing API key")
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise ValueError("invalid event ID")
        if not bookmakers or len(bookmakers) != len(set(bookmakers)):
            raise ValueError("bookmakers must be distinct and nonempty")
        if any(not re.fullmatch(r"[\w .-]{2,64}", name) for name in bookmakers):
            raise ValueError("invalid bookmaker identifier")
        if any(name not in rules for name in bookmakers):
            raise ValueError("missing audited settlement rules")
        if len({rules[book] for book in bookmakers}) != 1:
            raise ValueError("incompatible audited settlement rules")
        self.client = client
        self.api_key = api_key
        self.event_id = event_id
        self.bookmakers = bookmakers
        self.rules = rules
        self.journal = journal

    async def collect(self) -> list[Quote]:
        try:
            response = await self.client.get(
                "https://api.odds-api.io/v3/odds",
                params={
                    "apiKey": self.api_key,
                    "eventId": self.event_id,
                    "bookmakers": ",".join(self.bookmakers),
                },
                timeout=20,
                follow_redirects=False,
            )
        except httpx.RequestError:
            # Request objects and URLs can contain API secrets: never log or expose them.
            raise RuntimeError("Odds-API.io transport failure; no retry performed") from None
        if response.status_code != 200:
            raise RuntimeError(f"Odds-API.io HTTP {response.status_code}; no retry performed")
        received = datetime.now(UTC)
        try:
            # A provider may echo request credentials even in a successful response.
            payload = json.loads(response.text.replace(self.api_key, "[REDACTED]"))
            quotes = self.normalize(payload, received)
        except (ValueError, TypeError, KeyError):
            raise ValueError("invalid Odds-API.io response; normalization rejected") from None
        self.journal.append("odds-api-io", received, payload)
        return quotes

    def normalize(self, payload: dict, received: datetime) -> list[Quote]:
        """Fail closed for unknown/incomplete markets and missing source timestamps."""
        if not isinstance(payload, dict) or str(payload.get("id")) != str(self.event_id):
            raise ValueError("mismatched odds event ID")
        sport = payload["sport"]["slug"]
        event = Event(
            id=f"odds-api-io:{self.event_id}",
            sport=sport,
            home=payload["home"],
            away=payload["away"],
            starts_at=payload["date"],
        )
        if event.starts_at <= received:
            raise ValueError("pre-match event required")
        if payload.get("status", "pending") not in ("pending", "scheduled", "prematch"):
            raise ValueError("unsupported event status")
        raw_books = payload["bookmakers"]
        if not isinstance(raw_books, dict):
            raise TypeError("invalid bookmakers payload")
        quotes: list[Quote] = []
        for book in self.bookmakers:
            markets = raw_books.get(book, [])
            matches = [m for m in markets if m.get("name") == "ML"]
            if len(matches) > 1:
                raise ValueError("ambiguous moneyline market")
            if not matches:
                continue
            raw_market = matches[0]
            if "updatedAt" not in raw_market:
                raise ValueError("market source timestamp required")
            options = raw_market["odds"]
            if not isinstance(options, list) or len(options) != 1:
                raise ValueError("ambiguous moneyline price set")
            values = options[0]
            if not isinstance(values, dict):
                raise TypeError("invalid moneyline odds")
            keys = set(values) - {"href"}
            if keys not in ({"home", "away"}, {"home", "draw", "away"}):
                raise ValueError("incomplete or unsupported moneyline outcomes")
            # Soccer 1X2 cannot be safely interpreted as a two-outcome market.
            if sport == "football" and "draw" not in keys:
                raise ValueError("football match result requires the draw outcome")
            order = ("home", "draw", "away") if "draw" in keys else ("home", "away")
            market = Market(
                event=event,
                kind="moneyline",
                period="full",
                outcomes=order,
                rules=self.rules[book],
            )
            quotes.append(
                Quote(
                    market=market,
                    source=f"odds-api-io:{book}",
                    observed_at=raw_market["updatedAt"],
                    received_at=received,
                    outcomes=tuple(
                        Outcome(name=name, odds=float(values[name]), liquidity=0) for name in order
                    ),
                )
            )
        return quotes
