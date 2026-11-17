import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from .backtest import ResearchTrade, walk_forward
from .collectors import FixtureCollector
from .journal import Journal
from .replay import ReplayDataset, replay
from .reports import ReportRequest, report
from .research import analyze


async def run(args):
    if args.command == "report":
        return report(ReportRequest.model_validate_json(args.fixture.read_text(encoding="utf-8")))
    if args.command == "replay":
        return replay(ReplayDataset.model_validate_json(args.fixture.read_text(encoding="utf-8")))
    if args.command == "backtest":
        data = json.loads(args.fixture.read_text(encoding="utf-8"))
        return walk_forward(
            [ResearchTrade.model_validate(t) for t in data["trades"]],
            [datetime.fromisoformat(t) for t in data["boundaries"]],
        )
    if args.command == "scan-demo":
        from .paper import PaperLedger
        from .scanner import FeedSlot, ScanConfig, Scanner

        feed = FixtureCollector(args.fixture)
        quotes = await feed.collect()
        at = max(q.received_at for q in quotes)
        scanner = Scanner([FeedSlot("synthetic-fixture", feed)], ScanConfig())
        batch = await scanner.tick(at)
        ledger = PaperLedger(args.ledger)
        try:
            for signal in sorted(
                batch["signals"], key=lambda s: s.detail.get("conservative_ev", -1), reverse=True
            ):
                ledger.open(signal)
            return {
                "mode": "synthetic-paper-demo",
                "as_of": at.isoformat(),
                "signals": [s.model_dump(mode="json") for s in batch["signals"]],
                "health": batch["health"],
                "account": ledger.balances(),
            }
        finally:
            ledger.close()
    quotes = await FixtureCollector(args.fixture).collect()
    at = datetime.fromisoformat(args.at) if args.at else max(q.received_at for q in quotes)
    if args.journal:
        with Journal(args.journal) as journal:
            journal.append("fixture", at, json.loads(args.fixture.read_text(encoding="utf-8")))
    return analyze(quotes, at, args.budget)


def main():
    parser = argparse.ArgumentParser(description="Offline EV and arbitrage research")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("analyze")
    demo.add_argument("fixture", type=Path)
    demo.add_argument("--at", help="aware ISO timestamp; default latest fixture receipt")
    demo.add_argument("--budget", type=float, default=100)
    demo.add_argument("--journal", help="SQLite path or PostgreSQL DSN")
    backtest = sub.add_parser("backtest")
    backtest.add_argument("fixture", type=Path)
    history = sub.add_parser("replay")
    history.add_argument("fixture", type=Path)
    summary = sub.add_parser("report")
    summary.add_argument("fixture", type=Path)
    scan_demo = sub.add_parser("scan-demo")
    scan_demo.add_argument("fixture", type=Path)
    scan_demo.add_argument("--ledger", default=":memory:", help="local paper-only SQLite ledger")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), indent=2, ensure_ascii=False, allow_nan=False))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
