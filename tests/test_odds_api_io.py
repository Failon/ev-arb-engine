"""Synthetic, offline contract tests for the Odds-API.io V3 collector."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ev_arb_engine.collectors_odds_api_io import OddsAPIIOCollector
from ev_arb_engine.journal import Journal
from ev_arb_engine.models import SettlementRules


@pytest.fixture
def payload():
    received = datetime.now(UTC)
    return {
        "id": 123456,
        "home": "Club A",
        "away": "Club B",
        "date": (received + timedelta(days=2)).isoformat(),
        "sport": {"name": "Football", "slug": "football"},
        "bookmakers": {
            "Stake": [
                {
                    "name": "ML",
                    "updatedAt": (received - timedelta(seconds=4)).isoformat(),
                    "odds": [{"home": "2.20", "draw": "3.20", "away": "3.50"}],
                }
            ],
            "Parimatch UK": [
                {
                    "name": "ML",
                    "updatedAt": (received - timedelta(seconds=7)).isoformat(),
                    "odds": [{"home": "2.15", "draw": "3.30", "away": "3.60"}],
                }
            ],
        },
    }


@pytest.fixture
def rules():
    return {
        name: SettlementRules(
            ruleset="audited-identical-demo-1x2",
            overtime=False,
            abandonment="void",
        )
        for name in ("Stake", "Parimatch UK")
    }


def collector(client, journal, rules):
    return OddsAPIIOCollector(
        client, "synthetic-test-key", 123456, ("Stake", "Parimatch UK"), rules, journal
    )


@pytest.mark.asyncio
async def test_collect_normalizes_audited_three_way_markets(payload, rules, tmp_path):
    def handler(request):
        assert request.url.path == "/v3/odds"
        assert request.url.params["eventId"] == "123456"
        assert request.url.params["bookmakers"] == "Stake,Parimatch UK"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with Journal(tmp_path / "quotes.db") as journal:
            quotes = await collector(client, journal, rules).collect()
    assert len(quotes) == 2
    assert {q.source for q in quotes} == {"odds-api-io:Stake", "odds-api-io:Parimatch UK"}
    assert all(q.market.outcomes == ("home", "draw", "away") for q in quotes)
    assert all(q.outcomes[0].liquidity == 0 for q in quotes)
    assert all(q.observed_at <= q.received_at for q in quotes)
    assert quotes[0].market == quotes[1].market


def test_rejects_ambiguous_or_stale_contract_inputs(payload, rules, tmp_path):
    received = datetime.now(UTC)
    with Journal(tmp_path / "quotes.db") as journal:
        instance = collector(None, journal, rules)
        payload["bookmakers"]["Stake"][0].pop("updatedAt")
        with pytest.raises(ValueError, match="timestamp"):
            instance.normalize(payload, received)
        payload["bookmakers"]["Stake"][0]["updatedAt"] = received.isoformat()
        payload["bookmakers"]["Stake"][0]["odds"][0].pop("draw")
        with pytest.raises(ValueError, match="draw"):
            instance.normalize(payload, received)
        payload["bookmakers"]["Stake"][0]["odds"][0]["draw"] = "3.20"
        payload["id"] = 123457
        with pytest.raises(ValueError, match="ID"):
            instance.normalize(payload, received)


def test_requires_audited_rules_and_distinct_books(tmp_path, rules):
    with Journal(tmp_path / "quotes.db") as journal:
        with pytest.raises(ValueError, match="audited"):
            OddsAPIIOCollector(None, "key", 123456, ("Stake",), {}, journal)
        with pytest.raises(ValueError, match="distinct"):
            OddsAPIIOCollector(None, "key", 123456, ("Stake", "Stake"), rules, journal)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500, 302])
async def test_http_failures_do_not_leak_keys(status, rules, tmp_path):
    def handler(request):
        return httpx.Response(status, text="secret from remote provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with (
            Journal(tmp_path / "quotes.db") as journal,
            pytest.raises(RuntimeError, match=f"HTTP {status}") as exc,
        ):
            await collector(client, journal, rules).collect()
    assert "synthetic-test-key" not in str(exc.value)
    assert "secret from remote provider" not in str(exc.value)


@pytest.mark.asyncio
async def test_transport_failure_has_safe_error(rules, tmp_path):
    def handler(request):
        raise httpx.ConnectError("secret URL or token", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with (
            Journal(tmp_path / "quotes.db") as journal,
            pytest.raises(RuntimeError, match="transport failure") as exc,
        ):
            await collector(client, journal, rules).collect()
    assert "secret" not in str(exc.value)
