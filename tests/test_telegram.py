import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from ev_arb_engine.scanner import FeedSlot, ScanConfig, Scanner
from ev_arb_engine.telegram import (
    AlertState,
    TelegramBot,
    TelegramConfig,
    TelegramTransport,
)


class Feed:
    def __init__(self, quotes):
        self.quotes = quotes

    async def collect(self):
        return self.quotes


@pytest.fixture
async def signals(quotes, at):
    return (await Scanner([FeedSlot("synthetic", Feed(quotes))], ScanConfig()).tick(at))["signals"]


def config(**kwargs):
    return TelegramConfig(token=SecretStr("123:synthetic-secret"), chat_id=1234, **kwargs)


@pytest.mark.asyncio
async def test_format_delivery_dedup_and_persistence(signals, at, tmp_path):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    state = AlertState(str(tmp_path / "alerts.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBot(TelegramTransport(client, config()), state)
        for signal in signals:
            assert await bot.deliver(signal, at)
            text = sent[-1]["text"]
            assert "SYNTHETIC DEMO" in text
            assert "Verify price and settlement terms; not an executable guarantee." in text
            assert "observed=" in text and "received=" in text and "liquidity=" in text
            assert "123:synthetic-secret" not in text
        assert not await bot.deliver(signals[0], at)
        state.close()
        state = AlertState(str(tmp_path / "alerts.db"))
        assert not await TelegramBot(TelegramTransport(client, config()), state).deliver(
            signals[0], at
        )
    state.close()


@pytest.mark.asyncio
async def test_filters_stale_future_real_and_tampered_signals(signals, quotes, at):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r))
    ) as client:
        state = AlertState()
        bot = TelegramBot(TelegramTransport(client, config()), state)
        signal = next(s for s in signals if s.kind == "ev")
        assert not await bot.deliver(signal, at + timedelta(seconds=31))
        assert not await bot.deliver(signal, at - timedelta(seconds=1))
        assert not await bot.deliver(signal.model_copy(update={"synthetic": False}), at)
        assert not await bot.deliver(
            signal.model_copy(update={"detail": {**signal.detail, "conservative_ev": 999}}), at
        )
        incompatible = list(signal.quotes)
        incompatible[0] = incompatible[0].model_copy(
            update={"market": incompatible[0].market.model_copy(update={"period": "first_half"})}
        )
        assert not await bot.deliver(signal.model_copy(update={"quotes": tuple(incompatible)}), at)
        for cfg in [
            config(min_ev=100, min_arb_profit=100),
            config(sports=("unknown",)),
            config(markets=()),
        ]:
            assert not await TelegramBot(TelegramTransport(client, cfg), state).deliver(signal, at)
        assert calls == []
        state.close()


@pytest.mark.asyncio
async def test_cooldown_material_change_and_hourly_quota(signals, at):
    signal = next(s for s in signals if s.kind == "ev")
    state = AlertState()
    cfg = config(max_alerts_per_hour=2)
    state.record("key", signal, 0.1, at)
    changed = signal.model_copy(update={"fingerprint": "new-snapshot"})
    assert not state.allowed("key", changed, 0.101, at + timedelta(seconds=1), cfg)
    assert state.allowed("key", changed, 0.12, at + timedelta(seconds=1), cfg)
    state.record("key", changed, 0.12, at + timedelta(seconds=1))
    assert not state.allowed("other", signal, 1, at + timedelta(seconds=2), cfg)
    assert state.allowed("other", signal, 1, at + timedelta(hours=1, seconds=2), cfg)
    state.close()


@pytest.mark.asyncio
async def test_allowlisted_commands_and_poll_cursor():
    messages = []
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/pause"}},
        {"update_id": 2, "message": {"chat": {"id": 1234}, "text": "/pause"}},
        {"update_id": 3, "message": {"chat": {"id": 999}, "text": "/resume"}},
    ]

    def handler(request):
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": updates})
        messages.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        state = AlertState()
        bot = TelegramBot(TelegramTransport(client, config()), state)
        assert await bot.poll_once()
        assert state.setting("paused") == "1"
        assert state.setting("offset") == "4"
        assert len(messages) == 1 and messages[0]["chat_id"] == 1234
        updates[:] = [{"update_id": 4, "message": {"chat": {"id": 1234}, "text": "/resume"}}]
        assert await bot.poll_once()
        assert state.setting("paused") == "0"
        state.close()


@pytest.mark.asyncio
async def test_429_retry_and_secret_free_failures(monkeypatch, signals, at):
    calls = []

    async def no_wait(delay):
        calls.append(delay)

    monkeypatch.setattr("ev_arb_engine.telegram.asyncio.sleep", no_wait)
    responses = [
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}}),
        httpx.Response(200, json={"ok": True, "result": {}}),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: responses.pop(0))
    ) as client:
        sender = TelegramTransport(client, config())
        assert await sender.send("synthetic", valid_for=10)
        assert calls == [1]

    def failed(request):
        raise httpx.ConnectError("123:synthetic-secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as client:
        state = AlertState()
        bot = TelegramBot(TelegramTransport(client, config()), state)
        assert not await bot.deliver(signals[0], at)
        assert state.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
        assert "synthetic-secret" not in repr(bot.config)
        state.close()


@pytest.mark.asyncio
async def test_long_retry_after_suspends_without_blocking():
    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3600}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
        sender = TelegramTransport(client, config())
        assert not await sender.send("demo")
        assert not await sender.send("demo")
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_bounded_queue_does_not_block_collection(signals):
    async with httpx.AsyncClient() as client:
        state = AlertState()
        bot = TelegramBot(TelegramTransport(client, config()), state, queue_size=1)
        assert bot.submit(signals[0])
        assert not bot.submit(signals[0])
        assert not bot.submit(signals[1])
        state.close()


def test_env_missing_credentials_are_not_echoed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-invalid-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1234")
    with pytest.raises(ValueError) as exc:
        TelegramConfig.from_env()
    assert "secret-invalid-token" not in str(exc.value)


@pytest.mark.asyncio
async def test_retry_does_not_outlive_quote_freshness():
    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 5}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
        assert not await TelegramTransport(client, config()).send("demo", valid_for=1)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_dry_run_requires_no_token_or_network(monkeypatch):
    from pathlib import Path

    from ev_arb_engine.telegram_demo import demo

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    result = await demo(Path(__file__).parents[1] / "fixtures/quotes.json")
    assert result["mode"] == "synthetic-alert-preview"
    assert len(result["messages"]) == 3
    assert all("SYNTHETIC" in message for message in result["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 500, 302])
async def test_http_errors_are_isolated(status, signals, at):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json={"ok": False, "description": "synthetic-secret"})
        )
    ) as client:
        state = AlertState()
        assert not await TelegramBot(TelegramTransport(client, config()), state).deliver(
            signals[0], at
        )
        state.close()
