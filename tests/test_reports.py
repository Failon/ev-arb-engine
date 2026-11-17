from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError

from ev_arb_engine.api import app
from ev_arb_engine.reports import Observation, ReportRequest, report


def row(event, day, stake, profit, clv=None):
    return Observation(
        event_id=event,
        settled_at=datetime(2026, 1, day, tzinfo=UTC),
        stake=stake,
        profit=profit,
        clv=clv,
    )


def test_weighted_roi_drawdown_and_clv():
    request = ReportRequest(
        initial_balance=100,
        observations=(row("a", 1, 10, 10, 0.1), row("b", 2, 20, -20), row("c", 3, 30, 5, -0.1)),
    )
    result = report(request)
    assert result["roi"] == pytest.approx(-5 / 60)
    assert result["ending_balance"] == 95
    assert result["max_drawdown"] == 20
    assert result["max_drawdown_fraction"] == pytest.approx(20 / 110)
    assert result["mean_clv"] == 0
    assert result["clv_count"] == 2


def test_same_timestamp_grouping_and_order():
    a, b = row("a", 1, 100, -100), row("b", 1, 100, 100)
    first = report(ReportRequest(observations=(a, b)))
    second = report(ReportRequest(observations=(b, a)))
    assert first["equity_curve"] == second["equity_curve"]
    assert first["max_drawdown"] == 0


def test_bootstrap_reproducible_and_weighted():
    request = ReportRequest(observations=(row("a", 1, 10, 1), row("b", 2, 100, 10)))
    result = report(request)
    assert result["roi_bootstrap_95"] == pytest.approx([0.1, 0.1])
    assert result == report(request)


def test_empty_and_single_observation():
    empty = report(ReportRequest(observations=()))
    assert empty["roi"] is None
    assert empty["mean_clv"] is None
    assert empty["max_drawdown"] == 0
    assert empty["ending_balance"] == 1000
    assert report(ReportRequest(observations=(row("a", 1, 10, 1),)))["roi_bootstrap_95"] is None


def test_reject_duplicate_future_and_nonfinite():
    observation = row("a", 1, 10, 1)
    with pytest.raises(ValidationError, match="one row per event"):
        ReportRequest(observations=(observation, observation))
    with pytest.raises(ValidationError, match="historical"):
        ReportRequest(
            observations=(
                observation.model_copy(
                    update={"settled_at": datetime.now(UTC) + timedelta(days=1)}
                ),
            )
        )
    with pytest.raises(ValidationError):
        row("a", 1, 0, 1)
    with pytest.raises(ValidationError):
        row("a", 1, 10, float("nan"))


@pytest.mark.asyncio
async def test_dashboard_and_report_api():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert 'id="file"' in page.text
        data = ReportRequest(observations=(row("a", 1, 10, 1),)).model_dump(mode="json")
        response = await client.post("/reports", json=data)
        assert response.status_code == 200
        assert response.json()["roi"] == 0.1
        data["observations"][0]["stake"] = -1
        assert (await client.post("/reports", json=data)).status_code == 422
