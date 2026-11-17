"""Explicit synthetic fixture demo. No provider data is transmitted."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .collectors import FixtureCollector
from .scanner import FeedSlot, ScanConfig, Scanner
from .telegram import AlertState, TelegramBot, TelegramConfig, TelegramTransport, format_alert


async def demo(
    path: Path, *, send: bool = False, state_path: str = "alerts.db", iterations: int = 1
):
    if isinstance(iterations, bool) or not 1 <= iterations <= 100:
        raise ValueError("demo iterations must be between 1 and 100")
    quotes = await FixtureCollector(path).collect()
    if not quotes:
        raise ValueError("nonempty synthetic fixture required")
    baseline = max(q.received_at for q in quotes)
    if not send:
        # A dummy configuration is sufficient for local formatting; never used for HTTP.
        from pydantic import SecretStr

        cfg = TelegramConfig(token=SecretStr("unused"), chat_id=0)
        batch = await Scanner([FeedSlot("synthetic", FixtureCollector(path))], ScanConfig()).tick(
            baseline
        )
        return {
            "mode": "synthetic-alert-preview",
            "messages": [format_alert(s, baseline, cfg) for s in batch["signals"]],
        }

    config = TelegramConfig.from_env()
    # Rebase only the explicitly selected synthetic fixture; keep event starts fixed.
    initial_shift = datetime.now(UTC) - baseline
    markets = {
        q.market.model_dump_json(): q.market.model_copy(
            update={
                "event": q.market.event.model_copy(
                    update={"starts_at": q.market.event.starts_at + initial_shift}
                )
            }
        )
        for q in quotes
    }

    class SyntheticFeed:
        async def collect(self):
            shift = datetime.now(UTC) - baseline
            return [
                q.model_copy(
                    update={
                        "market": markets[q.market.model_dump_json()],
                        "observed_at": q.observed_at + shift,
                        "received_at": q.received_at + shift,
                    }
                )
                for q in quotes
            ]

    state = AlertState(state_path)
    async with httpx.AsyncClient() as client:
        bot = TelegramBot(TelegramTransport(client, config), state)
        worker = asyncio.create_task(bot.worker())
        polling = None
        try:
            await bot.poll_once()
            polling = asyncio.create_task(bot.polling(iterations=min(10000, iterations * 30)))
            scanner = Scanner([FeedSlot("synthetic", SyntheticFeed())], ScanConfig())

            async def on_batch(batch):
                for signal in batch["signals"]:
                    bot.submit(signal)

            await scanner.run(iterations=iterations, interval=60, on_batch=on_batch)
            await bot.queue.join()
            count = state.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
            return {"mode": "synthetic-telegram-demo", "successful_alerts_in_last_hour": count}
        finally:
            for task in (worker, polling):
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            state.close()
