from datetime import timedelta

import pytest

from ev_arb_engine.paper import PaperLedger
from ev_arb_engine.scanner import FeedSlot, ScanConfig, Scanner


class Feed:
    def __init__(self, quotes=None, failure=False):
        self.quotes, self.failure, self.calls = quotes or [], failure, 0

    async def collect(self):
        self.calls += 1
        if self.failure:
            raise RuntimeError("secret-provider-key")
        return self.quotes


@pytest.mark.asyncio
async def test_scan_isolation_freshness_quota_and_determinism(quotes, at):
    good, bad = Feed(quotes), Feed(failure=True)
    scanner = Scanner(
        [FeedSlot("good", good, requests_per_hour=1), FeedSlot("bad", bad)], ScanConfig()
    )
    batch = await scanner.tick(at)
    assert batch["signals"]
    assert "secret-provider-key" not in str(batch)
    assert batch["health"][1]["status"] == "unavailable"
    repeat = await scanner.tick(at + timedelta(seconds=1))
    assert repeat["signals"] == []
    assert good.calls == bad.calls == 1
    fresh = await Scanner([FeedSlot("good", Feed(quotes))], ScanConfig()).tick(at)
    assert [s.fingerprint for s in batch["signals"]] == [s.fingerprint for s in fresh["signals"]]
    stale = await Scanner([FeedSlot("good", Feed(quotes))], ScanConfig()).tick(
        at + timedelta(seconds=31)
    )
    assert stale["signals"] == []


@pytest.mark.asyncio
async def test_strict_market_grouping_and_no_future_receipts(quotes, at):
    changed = quotes[1].model_copy(
        update={"market": quotes[1].market.model_copy(update={"period": "first_half"})}
    )
    future = quotes[2].model_copy(update={"received_at": at + timedelta(seconds=1)})
    scanner = Scanner([FeedSlot("demo", Feed([quotes[0], changed, future]))], ScanConfig())
    batch = await scanner.tick(at)
    assert batch["signals"] == []
    assert batch["rejected_groups"] == 2
    with pytest.raises(ValueError, match="nondecreasing"):
        await scanner.tick(at - timedelta(seconds=1))
    with pytest.raises(ValueError, match="audited"):
        Scanner([FeedSlot("raw", Feed(quotes))], ScanConfig(), synthetic=False)


@pytest.mark.asyncio
async def test_paper_reserves_restarts_and_settles_once(quotes, at, tmp_path):
    batch = await Scanner([FeedSlot("demo", Feed(quotes))], ScanConfig()).tick(at)
    signal = next(s for s in batch["signals"] if s.kind == "ev")
    path = str(tmp_path / "paper.db")
    ledger = PaperLedger(path, initial_balance=12)
    assert ledger.open(signal, stake=10, fill_fraction=0.5, adverse_move=0.02)
    assert not ledger.open(signal)
    assert ledger.balances() == {"balance": 12, "reserved": 5, "available": 7}
    ledger.close()
    ledger = PaperLedger(path, initial_balance=999)
    assert ledger.balances()["available"] == 7
    event = signal.quotes[0].market.event
    settled = event.starts_at + timedelta(hours=3)
    with pytest.raises(ValueError, match="known settlement"):
        ledger.settle(event.id, "home", settled, now=at)
    with pytest.raises(ValueError, match="unknown settlement"):
        ledger.settle(event.id, "invalid", settled, now=settled)
    assert ledger.settle(event.id, None, settled, now=settled)
    assert not ledger.settle(event.id, None, settled, now=settled)
    assert ledger.balances() == {"balance": 12, "reserved": 0, "available": 12}
    with pytest.raises(ValueError, match="conflicting"):
        ledger.settle(event.id, "home", settled, now=settled)
    ledger.close()


@pytest.mark.asyncio
async def test_zero_liquidity_or_fill_creates_no_paper_trade(quotes, at):
    zero = [
        q.model_copy(
            update={"outcomes": tuple(o.model_copy(update={"liquidity": 0}) for o in q.outcomes)}
        )
        for q in quotes
    ]
    batch = await Scanner([FeedSlot("demo", Feed(zero))], ScanConfig()).tick(at)
    ledger = PaperLedger(":memory:")
    for signal in batch["signals"]:
        assert not ledger.open(signal)
    assert ledger.balances()["reserved"] == 0
    ledger.close()


@pytest.mark.asyncio
async def test_provider_retry_after_is_respected(quotes, at):
    from ev_arb_engine.collectors import ProviderRateLimit

    class Limited(Feed):
        async def collect(self):
            self.calls += 1
            raise ProviderRateLimit(7200)

    feed = Limited()
    scanner = Scanner([FeedSlot("limited", feed)], ScanConfig())
    assert (await scanner.tick(at))["health"][0]["status"] == "rate_limited"
    await scanner.tick(at + timedelta(hours=1))
    assert feed.calls == 1
    await scanner.tick(at + timedelta(hours=2))
    assert feed.calls == 2


@pytest.mark.asyncio
async def test_wall_clock_uses_post_collection_cutoff(quotes):
    from datetime import UTC, datetime

    class Current(Feed):
        async def collect(self):
            now = datetime.now(UTC)
            return [
                q.model_copy(
                    update={
                        "observed_at": now,
                        "received_at": now,
                        "market": q.market.model_copy(
                            update={
                                "event": q.market.event.model_copy(
                                    update={"starts_at": now + timedelta(days=1)}
                                )
                            }
                        ),
                    }
                )
                for q in quotes
            ]

    # Give all quotes the same event; collecting after tick begins must be usable.
    now = datetime.now(UTC)
    event = quotes[0].market.event.model_copy(update={"starts_at": now + timedelta(days=1)})

    class CurrentShared(Current):
        async def collect(self):
            batch = await super().collect()
            return [
                q.model_copy(update={"market": q.market.model_copy(update={"event": event})})
                for q in batch
            ]

    scanner = Scanner([FeedSlot("current", CurrentShared())], ScanConfig())
    assert (await scanner.tick())["signals"]


@pytest.mark.asyncio
@pytest.mark.parametrize("win", [True, False])
async def test_paper_win_and_loss_accounting(quotes, at, win):
    batch = await Scanner([FeedSlot("demo", Feed(quotes))], ScanConfig()).tick(at)
    signal = next(s for s in batch["signals"] if s.kind == "ev")
    ledger = PaperLedger(":memory:", initial_balance=100)
    assert ledger.open(signal, stake=10)
    quote = next(q for q in signal.quotes if q.source == signal.detail["source"])
    outcome = next(o for o in quote.outcomes if o.name == signal.detail["outcome"])
    winner = outcome.name if win else next(o.name for o in quote.outcomes if o.name != outcome.name)
    settled = quote.market.event.starts_at + timedelta(hours=1)
    assert ledger.settle(quote.market.event.id, winner, settled, now=settled)
    expected = 100 + 10 * (outcome.effective_odds - 1) if win else 90
    assert ledger.balances()["available"] == pytest.approx(expected)
    assert ledger.balances()["reserved"] == 0
    ledger.close()


@pytest.mark.asyncio
async def test_two_books_can_form_arb_but_not_ev_consensus(quotes, at):
    batch = await Scanner([FeedSlot("demo", Feed(quotes[:2]))], ScanConfig()).tick(at)
    assert batch["signals"]
    assert all(s.kind == "arbitrage" for s in batch["signals"])
