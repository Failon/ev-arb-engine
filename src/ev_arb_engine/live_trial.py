"""Private, bounded live-data research trial. No real orders or Telegram sends."""

import asyncio
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field

from .collectors_free_feeds import (
    BookAudit,
    PapiMarket,
    normalize_papi,
    normalize_the_odds,
    papi_event,
)
from .feed_http import BudgetHTTP, FeedError
from .models import StrictModel
from .research import analyze
from .scanner import fingerprint


def without_receipt(value):
    if isinstance(value, dict):
        return {key: without_receipt(item) for key, item in value.items() if key != "received_at"}
    if isinstance(value, list):
        return [without_receipt(item) for item in value]
    return value


class TrialConfig(StrictModel):
    days: int = Field(default=3, ge=1, le=7)
    interval_seconds: int = Field(default=3600, ge=900, le=86400)
    papi_budget: int = Field(default=200, ge=1, le=250)
    odds_budget: int = Field(default=400, ge=1, le=500)
    papi_tournament_id: int | None = Field(default=None, gt=0)
    odds_sport: Literal["soccer_epl", "upcoming"] = "upcoming"
    max_age: float = Field(default=30, gt=0, le=30)
    papi_books: dict[str, BookAudit] = Field(default_factory=dict)
    odds_books: dict[str, BookAudit] = Field(default_factory=dict)
    papi_market: PapiMarket | None = None


AUDIT_FIELDS = {"papi_books", "odds_books", "papi_market"}


def schedule_hash(config):
    return fingerprint(config.model_dump(mode="json", exclude=AUDIT_FIELDS))


class Trial:
    def __init__(
        self, db_path: str, config: TrialConfig, client: httpx.AsyncClient, keys: dict[str, str]
    ):
        if any(not keys.get(p, "").strip() for p in ("oddspapi", "the-odds-api")):
            raise ValueError(
                "missing local secrets: ODDSPAPI_API_KEY and THE_ODDS_API_KEY; no requests made"
            )
        self.db = sqlite3.connect(db_path)
        try:
            self.config, self.client, self.keys = config, client, keys
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS trial (id INTEGER PRIMARY KEY, config_hash TEXT, started TEXT, ends TEXT, last_tick TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS trial_payloads (provider TEXT, received_at TEXT, sha256 TEXT, payload TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS trial_ticks (at TEXT PRIMARY KEY, health TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS trial_signals (fingerprint TEXT PRIMARY KEY, at TEXT, kind TEXT, payload TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS trial_cache (key TEXT PRIMARY KEY, at TEXT, payload TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS trial_audits (hash TEXT PRIMARY KEY, accepted_at TEXT, payload TEXT)"
            )
            self.db.commit()
            saved = self.db.execute("SELECT config_hash FROM trial WHERE id=1").fetchone()
            if saved and saved[0] not in (
                schedule_hash(config),
                fingerprint(config.model_dump(mode="json")),
            ):
                raise ValueError("trial schedule/budget configuration is frozen")
            audit = config.model_dump(mode="json", include=AUDIT_FIELDS)
            previous = self.db.execute(
                "SELECT payload FROM trial_audits ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if previous:
                original = json.loads(previous[0])
                for field in ("papi_books", "odds_books"):
                    if any(
                        audit[field].get(slug) != book for slug, book in original[field].items()
                    ):
                        raise ValueError("accepted book audits cannot be removed or changed")
                if (
                    original["papi_market"] is not None
                    and original["papi_market"] != audit["papi_market"]
                ):
                    raise ValueError("accepted market audit cannot change")
            self.audit_hash = fingerprint(audit)
            self.feeds = {
                "oddspapi": BudgetHTTP(
                    client, self.db, "oddspapi", keys["oddspapi"], config.papi_budget
                ),
                "the-odds-api": BudgetHTTP(
                    client, self.db, "the-odds-api", keys["the-odds-api"], config.odds_budget
                ),
            }
            with self.db:
                if saved:
                    self.db.execute(
                        "UPDATE trial SET config_hash=? WHERE id=1", (schedule_hash(config),)
                    )
                self.db.execute(
                    "INSERT OR IGNORE INTO trial_audits VALUES (?, ?, ?)",
                    (
                        self.audit_hash,
                        datetime.now(UTC).isoformat(),
                        json.dumps(self._clean(audit)),
                    ),
                )
        except Exception:
            self.db.close()
            raise

    def _clean(self, value):
        raw = json.dumps(value, allow_nan=False, sort_keys=True)
        for key in self.keys.values():
            raw = raw.replace(key, "[REDACTED]")
        return json.loads(raw)

    def _archive(self, provider, payload, received):
        clean = self._clean(payload)
        with self.db:
            self.db.execute(
                "INSERT INTO trial_payloads VALUES (?, ?, ?, ?)",
                (
                    provider,
                    received.isoformat(),
                    fingerprint(clean),
                    json.dumps(clean, allow_nan=False),
                ),
            )

    async def discover(self):
        """Explicit metadata calls only, each charged against the same durable cap."""
        result = {}
        for provider, feed in self.feeds.items():
            endpoints = ("bookmakers", "markets") if provider == "oddspapi" else ("sports",)
            for endpoint in endpoints:
                try:
                    cache_key = f"{provider}:{endpoint}"
                    saved = self.db.execute(
                        "SELECT at,payload FROM trial_cache WHERE key=?", (cache_key,)
                    ).fetchone()
                    if saved and datetime.now(UTC) - datetime.fromisoformat(saved[0]) < timedelta(
                        hours=24
                    ):
                        result[cache_key] = json.loads(saved[1])
                        continue
                    data = await feed.get(endpoint)
                    self._archive(provider, data, datetime.now(UTC))
                    cache_key = f"{provider}:{endpoint}"
                    with self.db:
                        self.db.execute(
                            "INSERT OR REPLACE INTO trial_cache VALUES (?, ?, ?)",
                            (
                                cache_key,
                                datetime.now(UTC).isoformat(),
                                json.dumps(self._clean(data)),
                            ),
                        )
                    result[cache_key] = self._clean(data)
                except FeedError as exc:
                    result[f"{provider}:{endpoint}"] = {"status": str(exc)}
        return {"mode": "live-metadata-discovery", "data": result, "report": self.report()}

    async def _papi(self, at):
        feed = self.feeds["oddspapi"]
        cached = self.db.execute(
            "SELECT at,payload FROM trial_cache WHERE key='papi-fixtures'"
        ).fetchone()
        if cached is None or at - datetime.fromisoformat(cached[0]) >= timedelta(hours=6):
            filters = {"sportId": 10}
            if self.config.papi_tournament_id is not None:
                filters["tournamentId"] = self.config.papi_tournament_id
            fixtures = await feed.get(
                "fixtures",
                {
                    **filters,
                    "from": at.isoformat(),
                    "to": (at + timedelta(hours=24)).isoformat(),
                    "statusId": 0,
                    "hasOdds": "true",
                    "language": "en",
                },
            )
            if not isinstance(fixtures, list):
                raise FeedError("invalid fixtures response")
            self._archive("oddspapi", fixtures, datetime.now(UTC))
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO trial_cache VALUES ('papi-fixtures', ?, ?)",
                    (at.isoformat(), json.dumps(self._clean(fixtures))),
                )
        else:
            fixtures = json.loads(cached[1])
        eligible = []
        for item in fixtures:
            try:
                event = papi_event(item)
                if (
                    item.get("statusId") == 0
                    and item.get("hasOdds") is True
                    and event.starts_at > at
                ):
                    eligible.append((event.starts_at, event.id, item))
            except (KeyError, ValueError, TypeError):
                continue
        if not eligible:
            return [], {"status": "no_upcoming_fixtures", "raw_books": []}
        # One event per tick. Round-robin within the next day's fixtures limits spend.
        counter = self.db.execute("SELECT COUNT(*) FROM trial_ticks").fetchone()[0]
        expected = sorted(eligible, key=lambda row: row[:2])[counter % len(eligible)][2]
        data = await feed.get(
            "odds",
            {
                "fixtureId": expected["fixtureId"],
                "oddsFormat": "decimal",
                "language": "en",
                "verbosity": 3,
            },
        )
        received = datetime.now(UTC)
        self._archive("oddspapi", data, received)
        if papi_event(data) != papi_event(expected):
            raise FeedError("odds fixture differs from discovered metadata")
        return normalize_papi(data, received, self.config.papi_books, self.config.papi_market)

    async def _odds(self, _at):
        data = await self.feeds["the-odds-api"].get(
            f"sports/{self.config.odds_sport}/odds",
            {"regions": "eu", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"},
        )
        received = datetime.now(UTC)
        self._archive("the-odds-api", data, received)
        if not isinstance(data, list):
            raise FeedError("invalid odds response")
        return normalize_the_odds(data, received, self.config.odds_books)

    async def tick(self, at: datetime | None = None):
        at = at or datetime.now(UTC)
        if at.tzinfo is None:
            raise ValueError("aware clock required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT started,ends,last_tick FROM trial WHERE id=1").fetchone()
            if row is None:
                ends = at + timedelta(days=self.config.days)
                self.db.execute(
                    "INSERT INTO trial VALUES (1,?,?,?,NULL)",
                    (
                        schedule_hash(self.config),
                        at.isoformat(),
                        ends.isoformat(),
                    ),
                )
            else:
                started, ends_text, previous = row
                if at < datetime.fromisoformat(started):
                    raise ValueError("trial clock moved backwards")
                if at >= datetime.fromisoformat(ends_text):
                    return {"status": "completed", "report": self.report()}
                if (
                    previous
                    and (at - datetime.fromisoformat(previous)).total_seconds()
                    < self.config.interval_seconds
                ):
                    return {"status": "not_due", "report": self.report()}
            # Reserve this time slot before HTTP; restart cannot repeat it or extend the trial.
            self.db.execute("UPDATE trial SET last_tick=? WHERE id=1", (at.isoformat(),))

        async def fetch(provider, collect):
            try:
                quotes, health = await collect(at)
            except Exception:  # noqa: BLE001 - isolate provider and never expose response/URL
                return [], {
                    "provider": provider,
                    "status": "unavailable",
                    "spent": self.feeds[provider].spent,
                }
            cutoff = datetime.now(UTC)
            fresh = [q for q in quotes if q.available(cutoff, self.config.max_age)]
            return fresh, {
                **health,
                "provider": provider,
                "audit_revision": self.audit_hash,
                "normalized_count": len(quotes),
                "fresh_count": len(fresh),
                "stale_count": len(quotes) - len(fresh),
                "spent": self.feeds[provider].spent,
                "usage_headers": self.feeds[provider].last_usage,
            }

        batches = await asyncio.gather(
            fetch("oddspapi", self._papi), fetch("the-odds-api", self._odds)
        )
        cutoff = datetime.now(UTC)
        signals = []
        for quotes, health in batches:
            # Provider-native events stay separate until an explicit cross-provider audit.
            groups = defaultdict(list)
            for quote in quotes:
                groups[quote.market.model_dump_json()].append(quote)
            for group in groups.values():
                latest = {}
                for q in sorted(group, key=lambda q: (q.observed_at, q.received_at)):
                    latest[q.source] = q
                fresh = [
                    latest[source]
                    for source in sorted(latest)
                    if latest[source].available(cutoff, self.config.max_age)
                ]
                if len(fresh) < 2:
                    continue
                if len(fresh) >= 3:
                    result = analyze(fresh, cutoff)
                    for edge in result["ev"]:
                        if edge["conservative_ev"] > 0:
                            signals.append(
                                {
                                    "kind": "estimated_ev",
                                    "provider": health["provider"],
                                    "detail": edge,
                                    "quotes": [q.model_dump(mode="json") for q in fresh],
                                    "execution": "not_modeled; liquidity unknown",
                                    "accessible_from_russia": all(
                                        book.accessible_from_russia
                                        for book in (
                                            self.config.papi_books
                                            if health["provider"] == "oddspapi"
                                            else self.config.odds_books
                                        ).values()
                                        if book.canonical_id == edge["source"]
                                    ),
                                }
                            )
                legs = [
                    max(
                        ((q, q.outcomes[i]) for q in fresh), key=lambda pair: pair[1].effective_odds
                    )
                    for i in range(len(fresh[0].outcomes))
                ]
                inverse = math.fsum(1 / o.effective_odds for _, o in legs)
                if inverse < 1:
                    signals.append(
                        {
                            "kind": "theoretical_price_gap",
                            "provider": health["provider"],
                            "inverse_sum": inverse,
                            "legs": [
                                {"source": q.source, "outcome": o.name, "odds": o.odds}
                                for q, o in legs
                            ],
                            "market": fresh[0].market.model_dump(mode="json"),
                            "execution": "not_executable; liquidity unknown",
                        }
                    )
        with self.db:
            health = [h for _, h in batches]
            self.db.execute(
                "INSERT INTO trial_ticks VALUES (?, ?)",
                (at.isoformat(), json.dumps(self._clean(health))),
            )
            inserted = 0
            for signal in signals:
                clean = self._clean(signal)
                identity = without_receipt(clean)
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO trial_signals VALUES (?, ?, ?, ?)",
                    (fingerprint(identity), cutoff.isoformat(), clean["kind"], json.dumps(clean)),
                )
                inserted += cursor.rowcount
        return {
            "status": "sampled",
            "health": health,
            "new_candidates": inserted,
            "report": self.report(),
        }

    def report(self):
        return build_report(self.db)

    def close(self):
        self.db.close()


def build_report(db):
    health_rows = [
        json.loads(row[0]) for row in db.execute("SELECT health FROM trial_ticks ORDER BY at")
    ]
    inventory = defaultdict(set)
    for batch in health_rows:
        for item in batch:
            inventory[item["provider"]].update(item.get("raw_books", []))
    timeline = db.execute("SELECT started,ends,last_tick FROM trial WHERE id=1").fetchone()
    return {
        "mode": "live-research-only",
        "window": dict(zip(("started_at", "ends_at", "last_tick"), timeline)) if timeline else None,
        "ticks": len(health_rows),
        "budgets": {
            name: {"spent": spent, "cap": cap}
            for name, spent, cap in db.execute("SELECT provider,spent,cap FROM feed_budget")
        },
        "bookmaker_inventory": {name: sorted(books) for name, books in inventory.items()},
        "signals": {
            kind: count
            for kind, count in db.execute("SELECT kind,COUNT(*) FROM trial_signals GROUP BY kind")
        },
        "latest_health": health_rows[-1] if health_rows else [],
        "real_wagers": 0,
        "paper_fills": 0,
        "profit": None,
        "clv": None,
        "limitations": [
            "No results/closing line collected: candidate counts do not establish profitability.",
            "Russia registration and exact bookmaker editions require individual verification.",
            "Unknown settlement rules/market mapping are archived for discovery and never produce EV.",
        ],
    }


def local_keys(path: Path | None = None):
    names = {"ODDSPAPI_API_KEY", "THE_ODDS_API_KEY"}
    values = {name: os.getenv(name, "") for name in names}
    if path is not None and path.exists():
        seen = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            name, value = name.strip(), value.strip()
            if not separator or name not in names or name in seen:
                raise ValueError("invalid local secrets file; use each documented key name once")
            seen.add(name)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in (chr(34), chr(39)):
                value = value[1:-1]
            if not values[name]:
                values[name] = value
    return values


def preflight(path: Path | None = None):
    keys = local_keys(path)
    return {
        "mode": "local-preflight",
        "keys_configured": {
            name: bool(keys[name].strip()) for name in ("ODDSPAPI_API_KEY", "THE_ODDS_API_KEY")
        },
        "network_requests": 0,
    }


async def command(args):
    if args.command == "live-check":
        return preflight(args.secrets_file)
    if args.command == "live-report":
        # Reporting is offline, requires neither secrets nor a valid subscription.
        uri = Path(args.db).resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        try:
            return build_report(db)
        finally:
            db.close()
    config = (
        TrialConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
        if args.config
        else TrialConfig()
    )
    async with httpx.AsyncClient() as client:
        secrets = local_keys(args.secrets_file)
        trial = Trial(
            str(args.db),
            config,
            client,
            {
                "oddspapi": secrets["ODDSPAPI_API_KEY"],
                "the-odds-api": secrets["THE_ODDS_API_KEY"],
            },
        )
        try:
            if args.command == "live-discover":
                return await trial.discover()
            if args.once:
                return await trial.tick()
            while True:
                result = await trial.tick()
                if result["status"] != "not_due":
                    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
                if result["status"] == "completed":
                    return result
                await asyncio.sleep(30)
        finally:
            trial.close()
