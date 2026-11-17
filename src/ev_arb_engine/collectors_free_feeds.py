"""Documented V4 normalizers. Synthetic fixtures do not establish live coverage."""

from datetime import datetime

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from .collectors import TheOddsAPICollector
from .models import Event, Market, Outcome, Quote, SettlementRules, StrictModel


class BookAudit(StrictModel):
    canonical_id: str = Field(min_length=1)
    rules: SettlementRules
    evidence: str = Field(min_length=1)
    commission: float = Field(default=0, ge=0, lt=1)
    accessible_from_russia: bool = False
    sports: tuple[str, ...] = ("soccer",)


class PapiMarket(StrictModel):
    market_id: str = Field(min_length=1)
    sport_id: int = Field(default=10, gt=0)
    outcome_ids: dict[str, str]
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def complete(self):
        if tuple(self.outcome_ids) not in (("home", "away"), ("home", "draw", "away")):
            raise ValueError("complete canonical outcome mapping required")
        if len(set(self.outcome_ids.values())) != len(self.outcome_ids):
            raise ValueError("distinct provider outcome IDs required")
        if self.sport_id == 10 and "draw" not in self.outcome_ids:
            raise ValueError("soccer full-time result requires three outcomes")
        return self


STAMP = TypeAdapter(AwareDatetime)


def papi_event(item: dict) -> Event:
    return Event(
        id="oddspapi:" + item["fixtureId"],
        sport="soccer" if item["sportId"] == 10 else str(item["sportId"]),
        home=item["participant1Name"],
        away=item["participant2Name"],
        starts_at=item["startTime"],
    )


def normalize_papi(
    payload: dict, received: datetime, books: dict[str, BookAudit], mapping: PapiMarket | None
) -> tuple[list[Quote], dict]:
    event = papi_event(payload)
    health = {
        "raw_books": sorted(payload.get("bookmakerOdds", {})),
        "rejected_books": {},
        "timestamp_basis": "oldest outcome changedAt; conservative, not proof of last observation",
    }
    if mapping is None:
        return [], {**health, "status": "market_audit_required"}
    if (
        payload["sportId"] != mapping.sport_id
        or payload.get("statusId") != 0
        or event.starts_at <= received
        or payload.get("hasOdds") is not True
    ):
        return [], {**health, "status": "not_supported_prematch"}
    change_ages = []
    for board in payload.get("bookmakerOdds", {}).values():
        try:
            raw = board["markets"][mapping.market_id]
            if not (
                board.get("bookmakerIsActive") is True
                and board.get("suspended") is False
                and raw.get("marketActive") is True
                and set(raw["outcomes"]) == set(mapping.outcome_ids.values())
            ):
                continue
            players = [raw["outcomes"][oid]["players"]["0"] for oid in mapping.outcome_ids.values()]
            if not all(player.get("active") is True for player in players):
                continue
            ages = [
                (received - STAMP.validate_python(player["changedAt"])).total_seconds()
                for player in players
            ]
            if min(ages) >= 0:
                change_ages.append(max(ages))
        except (KeyError, ValueError, TypeError):
            continue
    health.update(
        {
            "raw_active_complete_boards": len(change_ages),
            "raw_recent_change_boards_30s": sum(age <= 30 for age in change_ages),
            "oldest_change_age_min_seconds": min(change_ages, default=None),
            "oldest_change_age_max_seconds": max(change_ages, default=None),
        }
    )
    quotes = []
    for slug, board in payload.get("bookmakerOdds", {}).items():
        if slug not in books:
            health["rejected_books"][slug] = "book_audit_required"
            continue
        try:
            if event.sport not in books[slug].sports:
                raise ValueError("sport settlement audit required")
            if board.get("bookmakerIsActive") is not True or board.get("suspended") is not False:
                raise ValueError("inactive bookmaker")
            raw = board["markets"][mapping.market_id]
            if raw.get("marketActive") is not True or set(raw["outcomes"]) != set(
                mapping.outcome_ids.values()
            ):
                raise ValueError("incomplete or extra outcomes")
            prices, stamps = [], []
            for name, outcome_id in mapping.outcome_ids.items():
                players = raw["outcomes"][outcome_id]["players"]
                if set(players) != {"0"} or players["0"].get("active") is not True:
                    raise ValueError("inactive or player-specific price")
                player = players["0"]
                if player.get("exchangeMeta") is not None:
                    raise ValueError("exchange pricing unsupported")
                stamp = STAMP.validate_python(player["changedAt"])
                if stamp > received:
                    raise ValueError("future source timestamp")
                stamps.append(stamp)
                prices.append(
                    Outcome(
                        name=name,
                        odds=player["price"],
                        commission=books[slug].commission,
                        liquidity=0,
                    )
                )
            quote = Quote(
                market=Market(
                    event=event,
                    kind="moneyline",
                    period="full",
                    outcomes=tuple(mapping.outcome_ids),
                    rules=books[slug].rules,
                ),
                source=books[slug].canonical_id,
                observed_at=min(stamps),
                received_at=received,
                outcomes=tuple(prices),
            )
            quotes.append(quote)
        except (ValueError, TypeError, KeyError):
            health["rejected_books"][slug] = "invalid_or_unsupported_market"
    return quotes, {
        **health,
        "status": "normalized" if quotes else "book_audit_required_or_rejected",
    }


def normalize_the_odds(
    payload: list, received: datetime, books: dict[str, BookAudit]
) -> tuple[list[Quote], dict]:
    quotes, raw_books, rejected = [], set(), {}
    normalizer = TheOddsAPICollector(
        None, "unused", "soccer_epl", {slug: book.rules for slug, book in books.items()}, None
    )
    for item in payload:
        for raw_book in item.get("bookmakers", []):
            slug = raw_book["key"]
            raw_books.add(slug)
            if slug not in books:
                rejected[slug] = "book_audit_required"
                continue
            try:
                sport = (
                    "soccer"
                    if item["sport_key"].startswith("soccer_")
                    else item["sport_key"].split("_")[0]
                )
                if sport not in books[slug].sports:
                    raise ValueError("sport settlement audit required")
                parsed = normalizer.normalize([{**item, "bookmakers": [raw_book]}], received)
                for quote in parsed:
                    if quote.market.event.starts_at <= received:
                        continue
                    if item["sport_key"].startswith("soccer_") and quote.market.outcomes != (
                        "home",
                        "draw",
                        "away",
                    ):
                        raise ValueError("soccer requires draw")
                    quotes.append(
                        quote.model_copy(
                            update={
                                "source": books[slug].canonical_id,
                                "outcomes": tuple(
                                    o.model_copy(update={"commission": books[slug].commission})
                                    for o in quote.outcomes
                                ),
                            }
                        )
                    )
            except (ValueError, TypeError, KeyError):
                rejected[slug] = "invalid_or_unsupported_market"
    return quotes, {
        "status": "normalized" if quotes else "book_audit_required_or_rejected",
        "raw_books": sorted(raw_books),
        "rejected_books": rejected,
        "timestamp_basis": "provider market last_update",
    }
