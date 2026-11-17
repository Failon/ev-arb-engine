# Odds-API.io V3 integration (research-only)

Status: **offline adapter implemented; live integration not verified** (2026-11-17).

Provider documentation:
- https://feedback.odds-api.io/en/help/articles/6035212-understanding-the-api-workflow
- https://feedback.odds-api.io/en/help/articles/1173410-quick-start-guide-get-up-and-running-in-5-minutes
- https://odds-api.io/sportsbooks

The catalogue lists Stake and Parimatch UK, but access to both for an actual event
depends on the subscriber's plan, permissions, geographical/market coverage and
the provider's availability. Do not claim the connection works until a permitted
live response has been checked. The API key must be supplied through a secret
environment variable (for example `ODDS_API_IO_KEY`), never committed.

`OddsAPIIOCollector` is a single-event, opt-in, read-only V3 adapter. It
requests the documented `/v3/odds` endpoint with `eventId`, `bookmakers`,
and `apiKey`, and normalizes **only** pre-match `ML` markets. It explicitly
requires audited, identical settlement rules across bookmakers to allow
cross-source comparison. Home/away/draw names and start time must match the
provider's event metadata exactly. The provider's `updatedAt` timestamp is
retained and quote ingestion time recorded separately.

Example (inside an async function; do not commit real credentials):

```python
import os
import httpx

from ev_arb_engine.collectors_odds_api_io import OddsAPIIOCollector
from ev_arb_engine.journal import Journal
from ev_arb_engine.models import SettlementRules

rules = {
    "Stake": SettlementRules(
        ruleset="REPLACE_WITH_AUDITED_RULES", overtime=False, abandonment="void"
    ),
    "Parimatch UK": SettlementRules(
        ruleset="REPLACE_WITH_AUDITED_RULES", overtime=False, abandonment="void"
    ),
}
# The sample ruleset name is a placeholder: verify both books' actual settlement
# policies before considering their prices comparable.

async with httpx.AsyncClient() as client:
    with Journal("quotes.db") as journal:
        collector = OddsAPIIOCollector(
            client,
            os.environ["ODDS_API_IO_KEY"],
            123456,  # Replace with a current event ID obtained from /v3/events
            ("Stake", "Parimatch UK"),
            rules,
            journal,
        )
        quotes = await collector.collect()
```

The engine assigns liquidity=0 because this provider's offered odds do not establish
actual acceptable stake size. This means no theoretical arbitration profit should
be presented as executable from these quotes alone. Missing timestamps, incomplete
soccer 1X2, unsupported market lines, multiple ML boards and unverified settlement
rules are rejected, rather than guessed.

**Next steps:** verify a real permitted response using the user's own provider
subscription; register audited settlement rules for the two bookmaker versions;
add source/bookmaker provenance deduplication across multiple aggregators;
implement documented event discovery with strict ID mapping; measure source
latency and stale rate. No real-money bet-placement code is included.
