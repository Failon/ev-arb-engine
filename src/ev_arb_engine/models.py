from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Event(StrictModel):
    id: str = Field(min_length=1)
    sport: str = Field(min_length=1)
    home: str = Field(min_length=1)
    away: str = Field(min_length=1)
    starts_at: AwareDatetime

    @model_validator(mode="after")
    def distinct(self):
        if self.home == self.away:
            raise ValueError("participants must differ")
        return self


class SettlementRules(StrictModel):
    # Explicit attestation from operator configuration, never inferred from API prices.
    ruleset: str = Field(min_length=1)
    overtime: bool
    abandonment: Literal["void", "official_result"]
    push: Literal["impossible"] = "impossible"


class Market(StrictModel):
    event: Event
    kind: Literal["moneyline", "total", "spread"]
    period: Literal["full", "first_half", "second_half"]
    line: Decimal | None = None  # spread is always expressed from home perspective
    outcomes: tuple[str, ...]
    rules: SettlementRules

    @model_validator(mode="after")
    def shape(self):
        expected = ("over", "under") if self.kind == "total" else ("home", "away")
        if self.kind == "moneyline":
            if self.line is not None or self.outcomes not in (expected, ("home", "draw", "away")):
                raise ValueError("moneyline requires ordered complete outcomes and no line")
        else:
            if self.outcomes != expected or self.line is None:
                raise ValueError("line market requires ordered complete outcomes and a line")
            if not self.line.is_finite() or abs(self.line % 1) != Decimal("0.5"):
                raise ValueError("MVP supports half lines only; pushes/quarter lines unsupported")
            if self.kind == "total" and self.line < 0:
                raise ValueError("negative total")
        if self.period != "full" and self.rules.overtime:
            raise ValueError("half period cannot include overtime")
        return self


class Outcome(StrictModel):
    name: str
    odds: float = Field(gt=1)
    commission: float = Field(default=0, ge=0, lt=1)
    liquidity: float = Field(ge=0)  # stake units, assumed or explicitly supplied

    @property
    def effective_odds(self) -> float:
        return 1 + (self.odds - 1) * (1 - self.commission)


class Quote(StrictModel):
    market: Market
    source: str = Field(min_length=1)
    observed_at: AwareDatetime
    received_at: AwareDatetime
    outcomes: tuple[Outcome, ...]

    @model_validator(mode="after")
    def coherent(self):
        if tuple(o.name for o in self.outcomes) != self.market.outcomes:
            raise ValueError("quote must contain exactly all market outcomes in canonical order")
        if self.observed_at > self.received_at:
            raise ValueError("source timestamp is in the future relative to receipt")
        return self

    def available(self, at: datetime, max_age: float) -> bool:
        return (
            self.received_at <= at < self.market.event.starts_at
            and 0 <= (at - self.observed_at).total_seconds() <= max_age
        )


def require_compatible(quotes: list[Quote]) -> Market:
    if not quotes:
        raise ValueError("empty quotes")
    market = quotes[0].market
    if any(q.market != market for q in quotes):
        raise ValueError("incompatible event, period, line, outcomes or settlement rules")
    return market
