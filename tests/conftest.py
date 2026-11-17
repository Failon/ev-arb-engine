import asyncio
from pathlib import Path

import pytest

from ev_arb_engine.collectors import FixtureCollector


@pytest.fixture
def quotes():
    return asyncio.run(
        FixtureCollector(Path(__file__).parents[1] / "fixtures/quotes.json").collect()
    )


@pytest.fixture
def at(quotes):
    return max(q.received_at for q in quotes)
