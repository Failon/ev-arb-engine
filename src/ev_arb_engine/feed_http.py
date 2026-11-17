"""Read-only provider HTTP with durable shared budgets and sanitized errors."""

import json
import math
import sqlite3
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import ClassVar

import httpx


class FeedError(RuntimeError):
    pass


class BudgetHTTP:
    HOSTS: ClassVar[dict[str, str]] = {
        "oddspapi": "https://api.oddspapi.io/v4",
        "the-odds-api": "https://api.the-odds-api.com/v4",
    }

    def __init__(
        self, client: httpx.AsyncClient, db: sqlite3.Connection, provider: str, key: str, cap: int
    ):
        if provider not in self.HOSTS or not key.strip() or not 1 <= cap <= 500:
            raise ValueError("valid provider, local API key and bounded budget required")
        self.client, self.db, self.provider, self.key = client, db, provider, key
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS feed_budget (provider TEXT PRIMARY KEY, spent INTEGER, cap INTEGER, blocked REAL)"
        )
        self.db.execute("INSERT OR IGNORE INTO feed_budget VALUES (?,0,?,0)", (provider, cap))
        self.db.commit()
        saved_cap = self.db.execute(
            "SELECT cap FROM feed_budget WHERE provider=?", (provider,)
        ).fetchone()[0]
        if saved_cap != cap:
            raise ValueError("budget cannot change when resuming an existing trial")
        self.last_usage: dict = {}

    @property
    def spent(self):
        return self.db.execute(
            "SELECT spent FROM feed_budget WHERE provider=?", (self.provider,)
        ).fetchone()[0]

    async def get(self, endpoint: str, params: dict | None = None, *, cost: int = 1):
        allowed = {
            "oddspapi": {"account", "sports", "bookmakers", "markets", "fixtures", "odds"},
            "the-odds-api": {"sports", "sports/soccer_epl/odds", "sports/upcoming/odds"},
        }
        if (
            endpoint not in allowed[self.provider]
            or cost not in (0, 1)
            or (cost == 0 and endpoint != "account")
        ):
            raise ValueError("unapproved read-only endpoint or cost")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            spent, cap, blocked = self.db.execute(
                "SELECT spent, cap, blocked FROM feed_budget WHERE provider=?", (self.provider,)
            ).fetchone()
            if datetime.now(UTC).timestamp() < blocked:
                raise FeedError("provider cooldown active")
            if spent + cost > cap:
                raise FeedError("local trial budget exhausted")
            self.db.execute(
                "UPDATE feed_budget SET spent=spent+? WHERE provider=?", (cost, self.provider)
            )
        query = dict(params or {})
        headers = {}
        if self.provider == "oddspapi":
            query["apiKey"] = self.key
        else:
            headers["x-api-key"] = self.key
        try:
            response = await self.client.get(
                self.HOSTS[self.provider] + "/" + endpoint,
                params=query,
                headers=headers,
                timeout=20,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise FeedError("provider transport failure; request budget retained") from None
        remaining = response.headers.get("x-requests-remaining")
        used = response.headers.get("x-requests-used")
        actual = response.headers.get("x-requests-last")
        self.last_usage = {
            k: int(v)
            for k, v in {"remaining": remaining, "used": used, "last_cost": actual}.items()
            if v is not None and v.isdigit()
        }
        with self.db:
            if actual and actual.isdigit() and int(actual) > cost:
                self.db.execute(
                    "UPDATE feed_budget SET spent=spent+? WHERE provider=?",
                    (int(actual) - cost, self.provider),
                )
            if response.status_code in (401, 403, 429) or remaining == "0":
                delay = 86400
                if response.status_code == 429:
                    try:
                        delay = float(response.headers.get("Retry-After", "86400"))
                        if not math.isfinite(delay) or delay < 0:
                            delay = 86400
                    except ValueError:
                        try:
                            delay = (
                                parsedate_to_datetime(response.headers["Retry-After"])
                                - datetime.now(UTC)
                            ).total_seconds()
                        except (ValueError, TypeError, KeyError, OverflowError):
                            delay = 86400
                    delay = max(3600, delay)
                self.db.execute(
                    "UPDATE feed_budget SET blocked=? WHERE provider=?",
                    (datetime.now(UTC).timestamp() + delay, self.provider),
                )
        if response.status_code != 200:
            raise FeedError(f"provider HTTP {response.status_code}; no automatic retry")
        try:
            # Do not persist even unexpected echoes of this subscription's API key.
            return json.loads(response.text.replace(self.key, "[REDACTED]"))
        except ValueError:
            raise FeedError("invalid provider JSON") from None
