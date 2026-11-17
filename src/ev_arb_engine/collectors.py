import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from .journal import Journal
from .models import Event, Market, Outcome, Quote, SettlementRules


class ProviderRateLimit(RuntimeError):
    def __init__(self, retry_after: float = 3600):
        self.retry_after = retry_after
        super().__init__("provider HTTP 429; collection suspended")


class Collector(Protocol):
    async def collect(self) -> list[Quote]: ...


class FixtureCollector:
    def __init__(self, path: Path):
        self.path = path

    async def collect(self) -> list[Quote]:
        data = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
        return [Quote.model_validate(q) for q in json.loads(data)]


class TheOddsAPICollector:
    """Official read-only V4 API, opt-in. Only explicitly mapped, audited h2h rules."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        sport: str,
        rules: dict[str, SettlementRules],
        journal: Journal,
        assumed_liquidity: float = 0,
    ):
        if not re.fullmatch(r"[a-z0-9_]+", sport):
            raise ValueError("invalid sport key")
        if assumed_liquidity < 0:
            raise ValueError("negative assumed liquidity")
        self.client, self.api_key, self.sport = client, api_key, sport
        self.rules, self.journal, self.liquidity = rules, journal, assumed_liquidity

    async def collect(self) -> list[Quote]:
        # No automatic retries: respect provider limits and avoid accidental quota spend.
        try:
            response = await self.client.get(
                f"https://api.the-odds-api.com/v4/sports/{self.sport}/odds/",
                params={
                    "apiKey": self.api_key,
                    "regions": "eu",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
                timeout=20,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise RuntimeError("odds API transport failure; no retry performed") from None
        if response.status_code != 200:
            # Do not include URL/exception request object: the query contains a secret.
            raise RuntimeError(f"odds API HTTP {response.status_code}; no retry performed")
        received = datetime.now(UTC)
        payload = response.json()
        self.journal.append("the-odds-api", received, payload)
        return self.normalize(payload, received)

    def normalize(self, payload: list, received: datetime) -> list[Quote]:
        quotes = []
        for item in payload:
            event = Event(
                id="the-odds-api:" + item["id"],
                sport=item["sport_key"],
                home=item["home_team"],
                away=item["away_team"],
                starts_at=item["commence_time"],
            )
            names = {event.home: "home", event.away: "away", "Draw": "draw"}
            for book in item["bookmakers"]:
                if book["key"] not in self.rules:
                    raise ValueError("missing audited settlement rules for " + book["key"])
                for raw_market in book["markets"]:
                    if raw_market["key"] != "h2h":
                        continue
                    raw = raw_market["outcomes"]
                    mapped = [names[o["name"]] for o in raw]
                    if len(set(mapped)) != len(mapped):
                        raise ValueError("duplicate outcome")
                    order = ("home", "draw", "away") if "draw" in mapped else ("home", "away")
                    by_name = {names[o["name"]]: o["price"] for o in raw}
                    market = Market(
                        event=event,
                        kind="moneyline",
                        period="full",
                        outcomes=order,
                        rules=self.rules[book["key"]],
                    )
                    quotes.append(
                        Quote(
                            market=market,
                            source=book["key"],
                            received_at=received,
                            observed_at=raw_market.get("last_update", book["last_update"]),
                            outcomes=tuple(
                                Outcome(name=n, odds=by_name[n], liquidity=self.liquidity)
                                for n in order
                            ),
                        )
                    )
        return quotes
