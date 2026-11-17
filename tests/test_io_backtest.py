import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ev_arb_engine.backtest import ResearchTrade, walk_forward
from ev_arb_engine.collectors import TheOddsAPICollector
from ev_arb_engine.journal import Journal

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_journal(tmp_path):
    with Journal(str(tmp_path / "raw.db")) as journal:
        for _ in range(2):
            journal.append("fixture", datetime.now(UTC), {"odds": 2.1})
        rows = journal.rows()
        assert len(rows) == 2
        assert rows[0][2] == rows[1][2]
        assert json.loads(rows[0][3]) == {"odds": 2.1}


@pytest.mark.asyncio
async def test_official_api_mock(tmp_path, quotes):
    payload = json.loads((FIXTURES / "provider.json").read_text())

    def handler(request):
        assert request.url.host == "api.the-odds-api.com"
        assert request.method == "GET"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with Journal(str(tmp_path / "raw.db")) as journal:
            collector = TheOddsAPICollector(
                client, "test-secret", "tennis", {"book_a": quotes[0].market.rules}, journal
            )
            result = await collector.collect()
            assert len(result) == 1
            assert result[0].outcomes[0].liquidity == 0
            assert len(journal.rows()) == 1
            assert "test-secret" not in str(journal.rows())
            collector.rules = {}
            with pytest.raises(ValueError, match="missing audited"):
                await collector.collect()
            assert len(journal.rows()) == 2  # raw persists even if normalization fails


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500, 302])
async def test_api_errors_no_secret(tmp_path, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with Journal(str(tmp_path / "raw.db")) as journal:
            collector = TheOddsAPICollector(client, "secret", "tennis", {}, journal)
            with pytest.raises(RuntimeError) as exc:
                await collector.collect()
            assert "secret" not in str(exc.value)
            assert len(calls) == 1


def test_walk_forward_no_future_labels():
    payload = json.loads((FIXTURES / "walk_forward.json").read_text())
    trades = [ResearchTrade.model_validate(t) for t in payload["trades"]]
    boundaries = [datetime.fromisoformat(t) for t in payload["boundaries"]]
    baseline = walk_forward(trades, boundaries)
    changed = [
        t.model_copy(update={"net_return": 100}) if t.decided_at >= boundaries[0] else t
        for t in trades
    ]
    assert walk_forward(changed, boundaries)[0]["threshold"] == baseline[0]["threshold"]
    delayed = [
        trades[0].model_copy(update={"settled_at": boundaries[0] + timedelta(days=1)}),
        *trades[1:],
    ]
    assert walk_forward(delayed, boundaries)[0]["status"] == "insufficient_training"
    with pytest.raises(ValueError, match="one research trade"):
        walk_forward([*trades, trades[0]], boundaries)


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_DSN"), reason="PostgreSQL DSN not configured")
def test_postgres():
    with Journal(os.environ["TEST_POSTGRES_DSN"]) as journal:
        before = len(journal.rows())
        journal.append("integration-test", datetime.now(UTC), {"test": True})
        assert len(journal.rows()) == before + 1
