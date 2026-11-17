"""Opt-in synthetic Telegram research demo; real-data alerts remain gated on #5."""

import asyncio
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import Field, SecretStr

from .arbitrage import Arbitrage
from .models import StrictModel, require_compatible
from .research import analyze
from .scanner import Signal, fingerprint

DISCLAIMER = "Verify price and settlement terms; not an executable guarantee."


class TelegramConfig(StrictModel):
    token: SecretStr
    chat_id: int
    min_ev: float = Field(default=0, ge=0)
    min_arb_profit: float = Field(default=0, ge=0)
    max_age: float = Field(default=30, ge=0, le=30)
    cooldown_seconds: int = Field(default=300, ge=0)
    material_change: float = Field(default=0.01, gt=0)
    max_alerts_per_hour: int = Field(default=20, ge=1, le=100)
    sports: tuple[str, ...] = ()
    markets: tuple[str, ...] = ("moneyline", "total", "spread")
    timezone: str = "UTC"

    @classmethod
    def from_env(cls):
        token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"-?[0-9]+", chat):
            raise ValueError("configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID locally")
        return cls(token=SecretStr(token), chat_id=int(chat))


class TelegramTransport:
    def __init__(self, client: httpx.AsyncClient, config: TelegramConfig):
        self.client, self.config = client, config
        self.blocked_until = 0.0

    async def request(
        self, method: Literal["sendMessage", "getUpdates"], payload: dict, *, valid_for: float = 30
    ) -> dict | None:
        if time.monotonic() < self.blocked_until:
            return None
        deadline = time.monotonic() + valid_for
        for attempt in range(2):
            if time.monotonic() >= deadline:
                return None
            try:
                response = await self.client.post(
                    f"https://api.telegram.org/bot{self.config.token.get_secret_value()}/{method}",
                    json=payload,
                    timeout=15,
                    follow_redirects=False,
                )
                data = response.json()
            except (httpx.RequestError, ValueError):
                # An ambiguous send failure is not retried to avoid duplicate deliveries.
                return None
            if not isinstance(data, dict):
                return None
            if response.status_code == 429 or data.get("error_code") == 429:
                parameters = data.get("parameters", {})
                delay = parameters.get("retry_after", 60) if isinstance(parameters, dict) else 60
                if isinstance(delay, bool) or not isinstance(delay, int) or delay < 1:
                    delay = 60
                self.blocked_until = time.monotonic() + delay
                if attempt == 0 and delay <= 30 and time.monotonic() + delay < deadline:
                    await asyncio.sleep(delay)
                    continue
                return None
            if response.status_code != 200 or data.get("ok") is not True:
                return None
            return data
        return None

    async def send(self, text: str, *, valid_for: float = 30) -> bool:
        return (
            await self.request(
                "sendMessage",
                {
                    "chat_id": self.config.chat_id,
                    "text": text,
                    "link_preview_options": {"is_disabled": True},
                    "protect_content": True,
                },
                valid_for=valid_for,
            )
            is not None
        )


class AlertState:
    """Single-instance SQLite cooldown, quota, pause and polling cursor state."""

    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, fingerprint TEXT, metric REAL, at REAL)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS deliveries (at REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        self.db.commit()

    def setting(self, key, default="0"):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))

    def allowed(self, key, signal, metric, at, config):
        if self.setting("paused") == "1":
            return False
        count = self.db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE at>?", (at.timestamp() - 3600,)
        ).fetchone()[0]
        if count >= config.max_alerts_per_hour:
            return False
        previous = self.db.execute(
            "SELECT fingerprint, metric, at FROM sent WHERE key=?", (key,)
        ).fetchone()
        if previous:
            if signal.fingerprint == previous[0] or at.timestamp() < previous[2]:
                return False
            if (
                at.timestamp() - previous[2] < config.cooldown_seconds
                and abs(metric - previous[1]) < config.material_change
            ):
                return False
        return True

    def record(self, key, signal, metric, at):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO sent VALUES (?, ?, ?, ?)",
                (key, signal.fingerprint, metric, at.timestamp()),
            )
            self.db.execute("DELETE FROM deliveries WHERE at<=?", (at.timestamp() - 3600,))
            self.db.execute("INSERT INTO deliveries VALUES (?)", (at.timestamp(),))

    def close(self):
        self.db.close()


def validate_signal(signal: Signal, at: datetime, config: TelegramConfig):
    if at.tzinfo is None or at < signal.decided_at or not signal.synthetic:
        return None  # Real-data dissemination is disabled until feed/licence validation.
    market = require_compatible(list(signal.quotes))
    if not all(q.available(at, config.max_age) for q in signal.quotes):
        return None
    if config.sports and market.event.sport not in config.sports:
        return None
    if market.kind not in config.markets:
        return None
    if signal.kind == "ev":
        result = analyze(list(signal.quotes), signal.decided_at)
        edge = next((e for e in result["ev"] if e == signal.detail), None)
        if edge is None or edge["conservative_ev"] <= config.min_ev:
            return None
        metric = edge["conservative_ev"]
        identity = (edge["source"], edge["outcome"])
    elif signal.kind == "arbitrage":
        plan = Arbitrage.model_validate(signal.detail)
        if not plan.legs or any(leg.quote not in signal.quotes for leg in plan.legs):
            return None
        if len(plan.legs) != len(market.outcomes) or {leg.outcome.name for leg in plan.legs} != set(
            market.outcomes
        ):
            return None
        if any(
            leg.outcome not in leg.quote.outcomes or leg.stake > leg.outcome.liquidity
            for leg in plan.legs
        ):
            return None
        total = sum(leg.stake for leg in plan.legs)
        worst = min(leg.stake * leg.outcome.effective_odds for leg in plan.legs) - total
        if (
            total != plan.total_stake
            or worst != plan.worst_profit
            or worst <= config.min_arb_profit
        ):
            return None
        metric, identity = worst, "all-outcomes"
    else:
        return None
    key = fingerprint(
        {"market": market.model_dump(mode="json"), "kind": signal.kind, "selection": identity}
    )
    return key, metric


def format_alert(signal: Signal, at: datetime, config: TelegramConfig) -> str:
    from zoneinfo import ZoneInfo

    market = signal.quotes[0].market
    event = market.event
    lines = [
        "SYNTHETIC DEMO • PAPER TRADING • research only",
        f"{event.sport}: {event.home} vs {event.away}",
        f"Start: {event.starts_at.astimezone(ZoneInfo(config.timezone)).isoformat()}",
        f"Market: {market.kind} / {market.period} / line={market.line}",
        f"Rules: {market.rules.ruleset}; overtime={market.rules.overtime}; abandonment={market.rules.abandonment}",
    ]
    if signal.kind == "ev":
        d = signal.detail
        lines.append(f"Selection: {d['outcome']} at {d['source']}")
        lines.append(
            f"Estimated fair p={d['fair_probability']:.4f}; references={d['reference_count']}; conservative EV={d['conservative_ev']:.2%}"
        )
        lines.append("Reference agreement is not calibrated confidence or independence.")
    else:
        lines.append(
            f"Theoretical arbitrage: modeled worst profit={signal.detail['worst_profit']:.2f}"
        )
    for quote in signal.quotes:
        prices = ", ".join(
            f"{o.name}={o.odds:g} (liquidity={o.liquidity:g}, fee={o.commission:g})"
            for o in quote.outcomes
        )
        lines.append(
            f"{quote.source}: {prices}; observed={quote.observed_at.isoformat()}; received={quote.received_at.isoformat()}; age={(at - quote.observed_at).total_seconds():g}s"
        )
    lines.append(
        "Liquidity is a modeled input; partial fills, delay and price moves may erase the edge."
    )
    return "\n".join(lines)[:3800] + "\n" + DISCLAIMER


class TelegramBot:
    def __init__(self, transport: TelegramTransport, state: AlertState, *, queue_size: int = 100):
        if queue_size < 1:
            raise ValueError("positive queue size required")
        self.transport, self.state, self.config = transport, state, transport.config
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.pending: set[str] = set()

    def submit(self, signal: Signal) -> bool:
        # Never wait for Telegram in the quote collection path.
        if self.queue.full() or signal.fingerprint in self.pending:
            return False
        self.pending.add(signal.fingerprint)
        self.queue.put_nowait(signal)
        return True

    async def deliver(self, signal: Signal, at: datetime) -> bool:
        try:
            validated = validate_signal(signal, at, self.config)
            if validated is None:
                return False
            key, metric = validated
            if not self.state.allowed(key, signal, metric, at, self.config):
                return False
            remaining = self.config.max_age - max(
                (at - q.observed_at).total_seconds() for q in signal.quotes
            )
            if not await self.transport.send(
                format_alert(signal, at, self.config), valid_for=remaining
            ):
                return False
            self.state.record(key, signal, metric, at)
            return True
        except Exception:  # noqa: BLE001 - alert failures must not stop research
            return False

    async def worker(self):
        while True:
            signal = await self.queue.get()
            try:
                await self.deliver(signal, datetime.now(UTC))
            finally:
                self.pending.discard(signal.fingerprint)
                self.queue.task_done()

    async def poll_once(self) -> bool:
        offset = int(self.state.setting("offset"))
        data = await self.transport.request(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 0,
                "allowed_updates": ["message"],
            },
        )
        if data is None or not isinstance(data.get("result"), list):
            return False
        for update in data["result"]:
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                continue
            message = update.get("message", {})
            if not isinstance(message, dict) or not isinstance(message.get("chat", {}), dict):
                self.state.set("offset", max(offset, update["update_id"] + 1))
                continue
            if message.get("chat", {}).get("id") == self.config.chat_id:
                text = message.get("text", "")
                command = (
                    text.split()[0].split("@")[0] if isinstance(text, str) and text.split() else ""
                )
                if command in ("/pause", "/resume"):
                    self.state.set("paused", int(command == "/pause"))
                if command in ("/start", "/help", "/status", "/pause", "/resume"):
                    paused = self.state.setting("paused") == "1"
                    await self.transport.send(
                        f"Synthetic paper research demo; paused={paused}. /start /help /status /pause /resume. No real wagers or live feed alerts."
                    )
            self.state.set("offset", max(offset, update["update_id"] + 1))
            offset = max(offset, update["update_id"] + 1)
        return True

    async def polling(self, *, iterations: int, interval: float = 2):
        if not 1 <= iterations <= 10000 or not 1 <= interval <= 60:
            raise ValueError("bounded polling configuration required")
        for index in range(iterations):
            await self.poll_once()
            if index + 1 < iterations:
                await asyncio.sleep(interval)
