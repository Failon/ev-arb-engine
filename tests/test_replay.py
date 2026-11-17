from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from ev_arb_engine.api import app
from ev_arb_engine.replay import (
    ReplayConfig,
    ReplayDataset,
    ReplayEvent,
    replay,
    replay_event,
    snapshot,
)


@pytest.fixture
def dataset():
    return ReplayDataset.model_validate_json(
        (Path(__file__).parents[1] / "fixtures/replay.json").read_text()
    )


def test_end_to_end(dataset):
    result = replay(dataset)
    assert result["filled"] == 8
    assert len(result["walk_forward"]) == 2
    assert all(r["clv"] is not None for r in result["records"])
    first = result["records"][0]
    assert first["profit"] == pytest.approx(first["stake"] * first["trade"]["net_return"])


def test_future_prices_and_results_cannot_change_decision(dataset):
    event = dataset.events[0]
    original = replay_event(event, dataset.config)
    changed = event.model_copy(
        update={
            "winner": "away",
            "quotes": tuple(
                q.model_copy(
                    update={
                        "outcomes": tuple(o.model_copy(update={"odds": 10}) for o in q.outcomes)
                    }
                )
                if q.received_at > event.decided_at + timedelta(seconds=1)
                else q
                for q in event.quotes
            ),
        }
    )
    other = replay_event(changed, dataset.config)
    for field in ("source", "outcome", "predicted_ev", "stake", "taken_odds"):
        assert other[field] == original[field]
    assert other["profit"] != original["profit"]
    assert other["clv"] != original["clv"]


def test_delayed_old_message_cannot_replace_newer(dataset):
    event = dataset.events[0]
    q = event.quotes[0]
    delayed = q.model_copy(
        update={
            "observed_at": q.observed_at - timedelta(seconds=10),
            "received_at": event.decided_at,
        }
    )
    selected = snapshot((*event.quotes, delayed), event.decided_at, 30)
    assert next(x for x in selected if x.source == q.source) == q


def test_partial_execution_and_missing_close(dataset):
    event = dataset.events[0]
    result = replay_event(event, ReplayConfig(fill_fraction=0.25))
    assert result["stake"] == 2.5
    assert replay_event(event, ReplayConfig(fill_fraction=0))["status"] == "unfilled"
    assert replay_event(event, ReplayConfig(latency_ms=60000))["status"] == "unfilled"
    no_close = event.model_copy(update={"quotes": event.quotes[:3]})
    result = replay_event(no_close, dataset.config)
    assert result["status"] == "filled"
    assert result["clv"] is None


def test_arrival_price_liquidity_and_void(dataset):
    event = dataset.events[0]
    baseline = replay_event(event, dataset.config)
    source = next(q for q in event.quotes if q.source == baseline["source"])
    moved = source.model_copy(
        update={
            "received_at": event.decided_at + timedelta(milliseconds=200),
            "observed_at": event.decided_at + timedelta(milliseconds=200),
            "outcomes": tuple(
                o.model_copy(update={"odds": 1.5, "liquidity": 1}) for o in source.outcomes
            ),
        }
    )
    changed = event.model_copy(update={"quotes": (*event.quotes, moved)})
    result = replay_event(changed, dataset.config)
    assert result["taken_odds"] == 1.5
    assert result["stake"] == 1
    assert result["predicted_ev"] == baseline["predicted_ev"]
    assert replay_event(event.model_copy(update={"winner": None}), dataset.config)["profit"] == 0


def test_validation(dataset):
    event = dataset.events[0].model_dump(mode="json")
    event["closing_at"] = event["settled_at"]
    with pytest.raises(ValidationError):
        ReplayEvent.model_validate(event)
    data = dataset.model_dump()
    data["events"] = [data["events"][0], data["events"][0]]
    with pytest.raises(ValidationError):
        ReplayDataset.model_validate(data)


@pytest.mark.asyncio
async def test_replay_http(dataset):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/replay", json=dataset.model_dump(mode="json"))
    assert response.status_code == 200
    assert response.json()["filled"] == 8
