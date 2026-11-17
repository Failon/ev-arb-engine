"""Synthetic provider contracts and multi-day pilot; all HTTP is mocked."""

import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from ev_arb_engine.collectors_free_feeds import (
    BookAudit,
    PapiMarket,
    normalize_papi,
    normalize_the_odds,
)
from ev_arb_engine.feed_http import BudgetHTTP, FeedError
from ev_arb_engine.live_trial import Trial, TrialConfig, command, local_keys, preflight
from ev_arb_engine.models import SettlementRules


@pytest.fixture
def data():
    now = datetime.now(UTC)
    prices = ((2.7, 3.2, 3.1), (2.0, 3.5, 4.5), (2.1, 3.4, 4.2))
    fixture = {
        "fixtureId": "synthetic-1",
        "participant1Name": "Synthetic Home",
        "participant2Name": "Synthetic Away",
        "sportId": 10,
        "statusId": 0,
        "hasOdds": True,
        "startTime": (now + timedelta(days=5)).isoformat(),
    }
    boards = {
        f"book-{i}": {
            "bookmakerIsActive": True,
            "suspended": False,
            "markets": {
                "101": {
                    "marketActive": True,
                    "outcomes": {
                        oid: {
                            "players": {
                                "0": {
                                    "active": True,
                                    "price": price,
                                    "changedAt": (now - timedelta(seconds=1)).isoformat(),
                                    "exchangeMeta": None,
                                }
                            }
                        }
                        for oid, price in zip(("101", "102", "103"), values)
                    },
                }
            },
        }
        for i, values in enumerate(prices)
    }
    papi = {**fixture, "bookmakerOdds": boards}
    odds = [
        {
            "id": "synthetic-odds-1",
            "sport_key": "soccer_epl",
            "home_team": fixture["participant1Name"],
            "away_team": fixture["participant2Name"],
            "commence_time": fixture["startTime"],
            "bookmakers": [
                {
                    "key": slug,
                    "last_update": now.isoformat(),
                    "markets": [
                        {
                            "key": "h2h",
                            "last_update": now.isoformat(),
                            "outcomes": [
                                {"name": name, "price": price}
                                for name, price in zip(
                                    (
                                        fixture["participant1Name"],
                                        "Draw",
                                        fixture["participant2Name"],
                                    ),
                                    values,
                                )
                            ],
                        }
                    ],
                }
                for slug, values in zip(boards, prices)
            ],
        }
    ]
    return {"now": now, "fixture": fixture, "papi": papi, "odds": odds}


@pytest.fixture
def config():
    books = {
        f"book-{i}": BookAudit(
            canonical_id=f"original-book-{i}",
            rules=SettlementRules(
                ruleset="synthetic-90min-no-overtime", overtime=False, abandonment="void"
            ),
            evidence="synthetic test only",
        )
        for i in range(3)
    }
    return TrialConfig(
        papi_books=books,
        odds_books=books,
        papi_market=PapiMarket(
            market_id="101",
            outcome_ids={"home": "101", "draw": "102", "away": "103"},
            evidence="synthetic catalogue only",
        ),
    )


KEYS = {"oddspapi": "papi-test-secret", "the-odds-api": "odds-test-secret"}


def handler_for(data, calls, bad_papi=False):
    def handler(request):
        calls.append(request)
        if request.url.host == "api.oddspapi.io":
            assert request.url.params["apiKey"] == KEYS["oddspapi"]
            if bad_papi:
                return httpx.Response(500, text=KEYS["oddspapi"])
            if request.url.path.endswith("fixtures"):
                return httpx.Response(200, json=[data["fixture"]])
            return httpx.Response(200, json=data["papi"])
        assert request.headers["x-api-key"] == KEYS["the-odds-api"]
        assert "apiKey" not in request.url.params
        return httpx.Response(
            200, json=data["odds"], headers={"x-requests-last": "1", "x-requests-remaining": "499"}
        )

    return handler


def test_papi_normalization_uses_oldest_price_change(data, config):
    old = data["now"] - timedelta(seconds=100)
    data["papi"]["bookmakerOdds"]["book-0"]["markets"]["101"]["outcomes"]["101"]["players"]["0"][
        "changedAt"
    ] = old.isoformat()
    quotes, health = normalize_papi(
        data["papi"], data["now"], config.papi_books, config.papi_market
    )
    assert len(quotes) == 3
    assert quotes[0].observed_at == old
    assert not quotes[0].available(data["now"], 30)
    assert all(o.liquidity == 0 for q in quotes for o in q.outcomes)
    assert "changedAt" in health["timestamp_basis"]


@pytest.mark.parametrize(
    "change", ["timestamp", "suspended", "inactive", "player", "extra_outcome", "exchange", "sport"]
)
def test_papi_rejects_unsafe_book(data, config, change):
    board = data["papi"]["bookmakerOdds"]["book-0"]
    market = board["markets"]["101"]
    player = market["outcomes"]["101"]["players"]["0"]
    if change == "timestamp":
        player["changedAt"] = (data["now"] + timedelta(minutes=1)).isoformat()
    elif change == "suspended":
        board["suspended"] = True
    elif change == "inactive":
        market["marketActive"] = False
    elif change == "player":
        market["outcomes"]["101"]["players"]["123"] = copy.deepcopy(player)
    elif change == "extra_outcome":
        market["outcomes"]["unexpected"] = copy.deepcopy(market["outcomes"]["101"])
    elif change == "exchange":
        player["exchangeMeta"] = {"lay": True}
    else:
        books = dict(config.papi_books)
        books["book-0"] = books["book-0"].model_copy(update={"sports": ("tennis",)})
        config = config.model_copy(update={"papi_books": books})
    quotes, health = normalize_papi(
        data["papi"], data["now"], config.papi_books, config.papi_market
    )
    assert len(quotes) == 2
    assert "book-0" in health["rejected_books"]


def test_unknown_audits_do_not_generate_quotes(data):
    quotes, health = normalize_papi(data["papi"], data["now"], {}, None)
    assert quotes == [] and health["status"] == "market_audit_required"
    quotes, health = normalize_the_odds(data["odds"], data["now"], {})
    assert quotes == [] and len(health["raw_books"]) == 3


def test_the_odds_incomplete_soccer_is_rejected(data, config):
    data["odds"][0]["bookmakers"][0]["markets"][0]["outcomes"].pop(1)
    quotes, health = normalize_the_odds(data["odds"], data["now"], config.odds_books)
    assert len(quotes) == 2
    assert "book-0" in health["rejected_books"]


@pytest.mark.asyncio
async def test_durable_cap_counts_failures_and_survives_restart(tmp_path):
    calls = []

    def failed(request):
        calls.append(request)
        raise httpx.ConnectError("papi-test-secret", request=request)

    path = str(tmp_path / "budget.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(failed)) as client:
        db = sqlite3.connect(path)
        feed = BudgetHTTP(client, db, "oddspapi", KEYS["oddspapi"], 1)
        with pytest.raises(FeedError, match="transport") as exc:
            await feed.get("odds")
        assert "papi-test-secret" not in str(exc.value)
        db.close()
        db = sqlite3.connect(path)
        feed = BudgetHTTP(client, db, "oddspapi", KEYS["oddspapi"], 1)
        with pytest.raises(FeedError, match="exhausted"):
            await feed.get("odds")
        assert len(calls) == 1
        with pytest.raises(ValueError, match="cannot change"):
            BudgetHTTP(client, db, "oddspapi", KEYS["oddspapi"], 2)
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 302, 500])
async def test_http_errors_safe_and_429_durable(tmp_path, status):
    calls = []

    def failure(request):
        calls.append(request)
        return httpx.Response(status, text="papi-test-secret", headers={"Retry-After": "172800"})

    db = sqlite3.connect(tmp_path / "budget.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(failure)) as client:
        feed = BudgetHTTP(client, db, "oddspapi", KEYS["oddspapi"], 10)
        with pytest.raises(FeedError, match=str(status)) as exc:
            await feed.get("odds")
        assert KEYS["oddspapi"] not in str(exc.value)
        if status in (401, 403, 429):
            with pytest.raises(FeedError, match="cooldown"):
                await feed.get("odds")
            assert len(calls) == 1
        assert feed.spent == 1
    db.close()


@pytest.mark.asyncio
async def test_integrated_real_mode_contract_resume_and_secrets(data, config, tmp_path):
    calls = []
    data["papi"]["debug"] = KEYS["oddspapi"] + KEYS["the-odds-api"]
    path = str(tmp_path / "trial.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(data, calls))) as client:
        trial = Trial(path, config, client, KEYS)
        first = await trial.tick(data["now"])
        assert first["status"] == "sampled"
        assert all(h["fresh_count"] == 3 for h in first["health"])
        assert first["new_candidates"] >= 2
        assert first["report"]["paper_fills"] == 0
        assert first["report"]["profit"] is None
        assert len(calls) == 3
        assert (await trial.tick(data["now"] + timedelta(minutes=1)))["status"] == "not_due"
        assert len(calls) == 3
        trial.close()
        assert all(
            key.encode() not in (tmp_path / "trial.db").read_bytes() for key in KEYS.values()
        )
        trial = Trial(path, config, client, KEYS)
        assert (await trial.tick(data["now"] + timedelta(days=3)))["status"] == "completed"
        assert len(calls) == 3
        trial.close()
        with pytest.raises(ValueError, match="frozen"):
            Trial(path, config.model_copy(update={"days": 4}), client, KEYS)
    offline = await command(SimpleNamespace(command="live-report", db=path))
    assert offline["ticks"] == 1
    assert offline["signals"]
    assert "papi-test-secret" not in json.dumps(offline)


@pytest.mark.asyncio
async def test_provider_outage_does_not_stop_other_source(data, config, tmp_path):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler_for(data, calls, bad_papi=True))
    ) as client:
        trial = Trial(str(tmp_path / "trial.db"), config, client, KEYS)
        result = await trial.tick(data["now"])
        assert result["health"][0]["status"] == "unavailable"
        assert result["health"][1]["fresh_count"] == 3
        assert result["new_candidates"] > 0
        trial.close()


@pytest.mark.asyncio
async def test_duplicate_original_book_does_not_inflate_consensus(data, config, tmp_path):
    books = dict(config.odds_books)
    books["book-1"] = books["book-0"]
    books["book-2"] = books["book-0"]
    config = config.model_copy(update={"odds_books": books, "papi_market": None})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(data, []))) as client:
        trial = Trial(str(tmp_path / "trial.db"), config, client, KEYS)
        result = await trial.tick(data["now"])
        assert result["new_candidates"] == 0
        trial.close()


def test_keys_missing_no_network_no_database(tmp_path, monkeypatch):
    for name in ("ODDSPAPI_API_KEY", "THE_ODDS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert not any(preflight()["keys_configured"].values())
    with pytest.raises(ValueError, match="no requests"):
        Trial(str(tmp_path / "absent.db"), TrialConfig(), None, {})
    assert not (tmp_path / "absent.db").exists()
    secret_file = tmp_path / "private.env"
    secret_file.write_text('ODDSPAPI_API_KEY="local-papi"\nTHE_ODDS_API_KEY=local-odds\n')
    assert all(preflight(secret_file)["keys_configured"].values())
    assert "local-papi" not in json.dumps(preflight(secret_file))
    secret_file.write_text("BAD_SECRET_NAME=do-not-echo\n")
    with pytest.raises(ValueError) as exc:
        local_keys(secret_file)
    assert "do-not-echo" not in str(exc.value)


@pytest.mark.asyncio
async def test_full_three_day_schedule_stays_inside_free_caps(data, config, tmp_path):
    calls = []
    path = str(tmp_path / "three-days.db")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(data, calls))) as client:
        trial = Trial(path, config, client, KEYS)
        for hour in range(72):
            result = await trial.tick(data["now"] + timedelta(hours=hour))
            assert result["status"] == "sampled"
            if hour:
                # Identical price snapshots with a new receipt must not inflate signals.
                assert result["new_candidates"] == 0
        assert trial.feeds["oddspapi"].spent == 84  # 72 odds + 12 fixture refreshes
        assert trial.feeds["the-odds-api"].spent == 72
        assert (await trial.tick(data["now"] + timedelta(hours=72)))["status"] == "completed"
        assert trial.report()["ticks"] == 72
        trial.close()


@pytest.mark.asyncio
async def test_changed_discovered_event_is_quarantined(data, config, tmp_path):
    data["papi"]["participant1Name"] = "Unmatched Team"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for(data, []))) as client:
        trial = Trial(str(tmp_path / "changed.db"), config, client, KEYS)
        result = await trial.tick(data["now"])
        assert result["health"][0]["status"] == "unavailable"
        assert result["health"][1]["fresh_count"] == 3
        trial.close()


@pytest.mark.asyncio
async def test_cost_headers_reconciled_and_empty_remaining_stops_next_call(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json=[], headers={"x-requests-last": "3", "x-requests-remaining": "0"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        db = sqlite3.connect(tmp_path / "credits.db")
        feed = BudgetHTTP(client, db, "the-odds-api", KEYS["the-odds-api"], 10)
        assert await feed.get("sports/upcoming/odds") == []
        assert feed.spent == 3
        with pytest.raises(FeedError, match="cooldown"):
            await feed.get("sports/upcoming/odds")
        assert len(calls) == 1
        db.close()


@pytest.mark.asyncio
async def test_discovery_cache_shares_budget_with_collection(data, config, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        trial = Trial(str(tmp_path / "metadata.db"), config, client, KEYS)
        await trial.discover()
        await trial.discover()
        assert len(calls) == 3
        assert trial.feeds["oddspapi"].spent == 2
        assert trial.feeds["the-odds-api"].spent == 1
        assert trial.report()["window"] is None  # Metadata does not start the 3-day clock.
        trial.close()
