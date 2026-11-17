"""Persistent single-process virtual ledger with reserved capital; no wager API."""

import math
import sqlite3
from datetime import datetime

from .scanner import Signal


class PaperLedger:
    def __init__(self, path: str, initial_balance: float = 1000):
        if not math.isfinite(initial_balance) or initial_balance <= 0:
            raise ValueError("positive finite balance required")
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY, initial REAL)")
        self.db.execute("INSERT OR IGNORE INTO account VALUES (1, ?)", (initial_balance,))
        self.db.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
            event_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, quote TEXT NOT NULL,
            selection TEXT NOT NULL, stake REAL NOT NULL, effective_odds REAL NOT NULL,
            opened_at TEXT NOT NULL, settled_at TEXT, winner TEXT, profit REAL)""")
        self.db.commit()

    def balances(self) -> dict:
        initial = self.db.execute("SELECT initial FROM account WHERE id=1").fetchone()[0]
        profit, reserved = self.db.execute(
            "SELECT COALESCE(SUM(profit),0), "
            "COALESCE(SUM(CASE WHEN settled_at IS NULL THEN stake ELSE 0 END),0) FROM paper_trades"
        ).fetchone()
        return {
            "balance": initial + profit,
            "reserved": reserved,
            "available": initial + profit - reserved,
        }

    def open(
        self,
        signal: Signal,
        *,
        stake: float = 10,
        fill_fraction: float = 1,
        adverse_move: float = 0,
    ) -> bool:
        if signal.kind != "ev" or signal.detail["conservative_ev"] <= 0:
            return False
        if not all(math.isfinite(x) for x in (stake, fill_fraction, adverse_move)) or not (
            stake > 0 and 0 <= fill_fraction <= 1 and 0 <= adverse_move < 1
        ):
            raise ValueError("invalid paper execution scenario")
        quote = next(q for q in signal.quotes if q.source == signal.detail["source"])
        if not quote.available(signal.decided_at, 30):
            return False
        outcome = next(o for o in quote.outcomes if o.name == signal.detail["outcome"])
        # Instantaneous modeled execution only; historical latency scenarios use replay.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            size = (
                math.floor(
                    min(stake * fill_fraction, outcome.liquidity, self.balances()["available"])
                    * 100
                )
                / 100
            )
            if size <= 0:
                return False
            odds = 1 + (outcome.effective_odds - 1) * (1 - adverse_move)
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO paper_trades VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                (
                    quote.market.event.id,
                    signal.fingerprint,
                    quote.model_dump_json(),
                    outcome.name,
                    size,
                    odds,
                    signal.decided_at.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def settle(
        self, event_id: str, winner: str | None, settled_at: datetime, *, now: datetime
    ) -> bool:
        from .models import Quote

        if settled_at.tzinfo is None or now.tzinfo is None or settled_at > now:
            raise ValueError("known settlement time required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT quote, selection, stake, effective_odds, opened_at, settled_at, winner FROM paper_trades WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown paper event")
            quote = Quote.model_validate_json(row[0])
            if winner is not None and winner not in quote.market.outcomes:
                raise ValueError("unknown settlement outcome")
            if settled_at < quote.market.event.starts_at or settled_at < datetime.fromisoformat(
                row[4]
            ):
                raise ValueError("settlement precedes event or decision")
            if row[5] is not None:
                if row[5] != settled_at.isoformat() or row[6] != winner:
                    raise ValueError("conflicting settlement")
                return False
            profit = 0 if winner is None else row[2] * ((row[3] if winner == row[1] else 0) - 1)
            self.db.execute(
                "UPDATE paper_trades SET settled_at=?, winner=?, profit=? WHERE event_id=?",
                (settled_at.isoformat(), winner, profit, event_id),
            )
            return True

    def close(self):
        self.db.close()
