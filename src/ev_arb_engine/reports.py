"""Retrospective statistics for completed virtual experiments, without signal generation."""

import math
import random
from collections import defaultdict
from datetime import UTC, datetime
from statistics import fmean

from pydantic import AwareDatetime, Field, model_validator

from .models import StrictModel


class Observation(StrictModel):
    event_id: str = Field(min_length=1)
    settled_at: AwareDatetime
    stake: float = Field(gt=0)
    profit: float
    clv: float | None = None


class ReportRequest(StrictModel):
    observations: tuple[Observation, ...] = Field(max_length=2000)
    initial_balance: float = Field(default=1000, gt=0)
    bootstrap_samples: int = Field(default=1000, ge=100, le=2000)
    seed: int = Field(default=42, ge=0)

    @model_validator(mode="after")
    def historical_unique(self):
        if any(o.settled_at > datetime.now(UTC) for o in self.observations):
            raise ValueError("reports accept completed historical observations only")
        ids = [o.event_id for o in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("aggregate correlated observations into one row per event")
        return self


def percentile(ordered: list[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def report(request: ReportRequest) -> dict:
    observations = request.observations
    stake = math.fsum(o.stake for o in observations)
    profit = math.fsum(o.profit for o in observations)
    clvs = [o.clv for o in observations if o.clv is not None]
    settlements = defaultdict(list)
    for o in observations:
        settlements[o.settled_at].append(o.profit)
    balance = peak = request.initial_balance
    max_drawdown = max_drawdown_fraction = 0.0
    curve = []
    for timestamp, profits in sorted(settlements.items()):
        balance += math.fsum(profits)
        peak = max(peak, balance)
        drawdown = peak - balance
        max_drawdown = max(max_drawdown, drawdown)
        max_drawdown_fraction = max(max_drawdown_fraction, drawdown / peak)
        curve.append(
            {"settled_at": timestamp.isoformat(), "balance": balance, "drawdown": drawdown}
        )
    interval = None
    if len(observations) >= 2:
        rng = random.Random(request.seed)
        estimates = []
        for _ in range(request.bootstrap_samples):
            sample = rng.choices(observations, k=len(observations))
            estimates.append(
                math.fsum(o.profit for o in sample) / math.fsum(o.stake for o in sample)
            )
        estimates.sort()
        interval = [percentile(estimates, 0.025), percentile(estimates, 0.975)]
    return {
        "mode": "historical-report",
        "events": len(observations),
        "total_stake": stake,
        "profit": profit,
        "roi": profit / stake if stake else None,
        "initial_balance": request.initial_balance,
        "ending_balance": balance,
        "max_drawdown": max_drawdown,
        "max_drawdown_fraction": max_drawdown_fraction,
        "mean_clv": fmean(clvs) if clvs else None,
        "clv_count": len(clvs),
        "roi_bootstrap_95": interval,
        "bootstrap_seed": request.seed,
        "equity_curve": curve,
        "limitations": [
            "Event-level percentile bootstrap assumes independent, representative events.",
            "Small samples and correlated events can make the interval misleading.",
            "Equity is settled P&L; open exposure and capital reservation are not modeled.",
            "Historical ROI and CLV are diagnostics, not forecasts or current recommendations.",
        ],
    }
