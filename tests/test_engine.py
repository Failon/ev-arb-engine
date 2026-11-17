from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from ev_arb_engine.api import app
from ev_arb_engine.arbitrage import ExecutionScenario, scan, simulate
from ev_arb_engine.models import Market, Outcome, Quote, require_compatible
from ev_arb_engine.pricing import clv, consensus, conservative_ev, devig
from ev_arb_engine.research import analyze


@pytest.mark.parametrize("method", ["multiplicative", "additive", "power"])
@pytest.mark.parametrize("odds", [[1.9, 1.9], [2, 3, 4], [2.2, 2.2]])
def test_devig(method, odds):
    p = devig(odds, method)
    assert sum(p) == pytest.approx(1)
    assert all(0 < v < 1 for v in p)


@pytest.mark.parametrize("odds", [[1, 2], [float("nan"), 2], [2], [float("inf"), 3]])
def test_bad_odds(odds):
    with pytest.raises(ValueError):
        devig(odds)


@pytest.mark.parametrize(
    "change",
    [
        {"period": "first_half"},
        {"rules": {"ruleset": "other", "overtime": False, "abandonment": "void"}},
        {"outcomes": ["home", "draw", "away"]},
    ],
)
def test_compatibility(quotes, change):
    data = quotes[0].market.model_dump()
    data.update(change)
    changed = quotes[0].model_copy(update={"market": Market.model_validate(data)})
    with pytest.raises(ValueError, match="incompatible"):
        require_compatible([quotes[0], changed])


@pytest.mark.parametrize("line", ["2", "2.25", "NaN"])
def test_unsupported_lines(quotes, line):
    data = quotes[0].market.model_dump()
    data.update(kind="total", line=line, outcomes=("over", "under"))
    with pytest.raises(ValidationError):
        Market.model_validate(data)


def test_different_lines_and_events(quotes):
    data = quotes[0].market.model_dump()
    data.update(kind="total", line="2.5", outcomes=("over", "under"))
    a = Market.model_validate(data)
    data["line"] = "3.5"
    b = Market.model_validate(data)
    with pytest.raises(ValueError):
        require_compatible(
            [quotes[0].model_copy(update={"market": a}), quotes[0].model_copy(update={"market": b})]
        )
    data = quotes[0].market.model_dump()
    data["event"]["id"] = "different"
    with pytest.raises(ValueError):
        require_compatible(
            [quotes[0], quotes[0].model_copy(update={"market": Market.model_validate(data)})]
        )


def test_consensus_excludes_target(quotes):
    p = consensus(quotes, quotes[0].source)
    altered = quotes[0].model_copy(
        update={"outcomes": tuple(o.model_copy(update={"odds": 100}) for o in quotes[0].outcomes)}
    )
    assert consensus([altered, *quotes[1:]], quotes[0].source) == p
    with pytest.raises(ValueError):
        consensus([quotes[0], quotes[1], quotes[1]], quotes[0].source)


def test_ev_clv():
    o = Outcome(name="home", odds=2.2, commission=0.05, liquidity=100)
    assert conservative_ev(o, 0.5, 0.02, 0.01) == pytest.approx(0.48 * 2.14 - 1.01)
    assert clv(o, 0.5) == pytest.approx(0.07)


def test_arbitrage_and_costs(quotes, at):
    plan = scan(quotes, at)
    assert plan and plan.worst_profit > 0
    assert plan.total_stake <= 100
    assert all(leg.stake <= leg.outcome.liquidity for leg in plan.legs)
    assert scan(quotes, at, cost=20) is None
    assert scan(quotes, at + timedelta(seconds=60)) is None
    assert scan(quotes, at - timedelta(seconds=60)) is None
    high_fee = [
        q.model_copy(
            update={"outcomes": tuple(o.model_copy(update={"commission": 0.5}) for o in q.outcomes)}
        )
        for q in quotes
    ]
    assert scan(high_fee, at) is None


def test_partial_fill_can_lose(quotes, at):
    plan = scan(quotes, at)
    missing_leg = [q for q in quotes if q.source != plan.legs[0].quote.source]
    result = simulate(plan, at, missing_leg, ExecutionScenario())
    assert result["worst_profit"] < 0
    assert simulate(plan, at, quotes, ExecutionScenario(latency_ms=60000))["total_stake"] == 0


def test_liquidity_and_future_state(quotes, at):
    plan = scan(quotes, at)
    future = [
        q.model_copy(
            update={
                "received_at": at + timedelta(seconds=10),
                "observed_at": at + timedelta(seconds=10),
            }
        )
        for q in quotes
    ]
    assert simulate(plan, at, future, ExecutionScenario())["total_stake"] == 0
    thin = [
        q.model_copy(
            update={"outcomes": tuple(o.model_copy(update={"liquidity": 1}) for o in q.outcomes)}
        )
        for q in quotes
    ]
    result = simulate(plan, at, thin, ExecutionScenario())
    assert all(f["stake"] <= 1 for f in result["fills"])


def test_quote_validation(quotes):
    data = quotes[0].model_dump()
    data["outcomes"] = data["outcomes"][:1]
    with pytest.raises(ValidationError):
        Quote.model_validate(data)


def test_superseded_prices_cannot_create_arbitrage(quotes, at):
    updated = [
        q.model_copy(
            update={
                "observed_at": at,
                "received_at": at,
                "outcomes": tuple(o.model_copy(update={"odds": 1.8}) for o in q.outcomes),
            }
        )
        for q in quotes
    ]
    assert scan([*quotes, *updated], at) is None


def test_delayed_quote_does_not_overwrite_fresh_analysis(quotes, at):
    old = quotes[0].model_copy(
        update={
            "observed_at": at - timedelta(seconds=20),
            "received_at": at,
            "outcomes": tuple(o.model_copy(update={"odds": 5}) for o in quotes[0].outcomes),
        }
    )
    assert analyze([*quotes, old], at) == analyze(quotes, at)


@pytest.mark.asyncio
async def test_api(quotes, at):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).json()["mode"] == "research-only"
        payload = {"quotes": [q.model_dump(mode="json") for q in quotes], "as_of": at.isoformat()}
        assert (await client.post("/analyze", json=payload)).status_code == 200
        payload["budget"] = -1
        assert (await client.post("/analyze", json=payload)).status_code == 422
    assert analyze(quotes, at)["arbitrage"]["worst_profit"] > 0


def test_reference_disagreement_diagnostics(quotes, at):
    # A distorted reference must appear in the probability range, not be
    # silently hidden by the consensus average.
    baseline = analyze(quotes, at)
    candidate = baseline["ev"][0]
    assert candidate["reference_count"] == len(quotes) - 1
    assert candidate["reference_probability_min"] <= candidate["fair_probability"]
    assert candidate["fair_probability"] <= candidate["reference_probability_max"]
    assert candidate["min_reference_conservative_ev"] <= candidate["conservative_ev"]
    assert isinstance(candidate["references_agree_on_positive_ev"], bool)

    source = quotes[1]
    altered = source.model_copy(
        update={
            "outcomes": tuple(
                o.model_copy(update={"odds": (1.15 if i == 0 else 12.0)})
                for i, o in enumerate(source.outcomes)
            )
        }
    )
    changed = analyze([quotes[0], altered, *quotes[2:]], at)
    edge = next(
        x for x in changed["ev"]
        if x["source"] == quotes[0].source and x["outcome"] == quotes[0].outcomes[0].name
    )
    assert edge["reference_probability_min"] < edge["reference_probability_max"]
    assert edge["reference_count"] == len(quotes) - 1
    assert edge["min_reference_conservative_ev"] <= edge["conservative_ev"]


def test_reference_diagnostics_match_individual_devigged_sources(quotes, at):
    results = analyze(quotes, at)
    target = quotes[0]
    references = [q for q in quotes if q.source != target.source]
    probabilities = [devig([o.odds for o in q.outcomes], "power") for q in references]
    for index, outcome in enumerate(target.outcomes):
        edge = next(
            row for row in results["ev"]
            if row["source"] == target.source and row["outcome"] == outcome.name
        )
        values = [p[index] for p in probabilities]
        assert edge["reference_count"] == 2
        assert edge["reference_probability_min"] == pytest.approx(min(values))
        assert edge["reference_probability_max"] == pytest.approx(max(values))
        assert edge["fair_probability"] == pytest.approx(sum(values) / len(values))
        assert edge["min_reference_conservative_ev"] == pytest.approx(
            conservative_ev(outcome, min(values))
        )
        assert edge["references_agree_on_positive_ev"] is all(
            conservative_ev(outcome, value) > 0 for value in values
        )


def test_future_and_superseded_prices_do_not_change_reference_diagnostics(quotes, at):
    baseline = analyze(quotes, at)["ev"]
    future = quotes[1].model_copy(
        update={
            "observed_at": at + timedelta(seconds=3),
            "received_at": at + timedelta(seconds=3),
            "outcomes": tuple(
                o.model_copy(update={"odds": 100.0}) for o in quotes[1].outcomes
            ),
        }
    )
    delayed = quotes[1].model_copy(
        update={
            "observed_at": at - timedelta(seconds=10),
            "received_at": at,
            "outcomes": tuple(
                o.model_copy(update={"odds": 100.0}) for o in quotes[1].outcomes
            ),
        }
    )
    assert analyze([*quotes, future, delayed], at)["ev"] == baseline


def test_three_way_reference_diagnostics(quotes, at):
    market = quotes[0].market.model_copy(
        update={"outcomes": ("home", "draw", "away")}
    )
    prices = ((2.5, 3.2, 3.1), (2.8, 3.0, 2.9), (2.6, 3.4, 2.7))
    three_way_quotes = [
        q.model_copy(
            update={
                "market": market,
                "outcomes": tuple(
                    Outcome(name=name, odds=odd, liquidity=100)
                    for name, odd in zip(market.outcomes, book_prices)
                ),
            }
        )
        for q, book_prices in zip(quotes, prices)
    ]
    result = analyze(three_way_quotes, at)
    assert len(result["ev"]) == 9
    assert all(item["reference_count"] == 2 for item in result["ev"])
    assert {item["outcome"] for item in result["ev"]} == {"home", "draw", "away"}
