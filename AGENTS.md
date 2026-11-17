# EV-ARB Engine — instructions for coding agents

This repository is an offline-first Python 3.12+ sports-odds research and paper-trading engine.
It is **not** a real-money betting execution product. Never implement wager submission,
account automation, unauthorized scraping, CAPTCHA/anti-bot bypass or geo-restriction evasion.

## Before editing
1. Read `README.md`, `VERIFICATION.md`, `pyproject.toml` and the specific GitHub issue.
2. Inspect existing implementation and tests; don't duplicate implemented features.
3. Work in a dedicated feature branch, with one focused PR and linked issue.
4. Keep backwards compatibility for CLI, JSON formats and the FastAPI endpoints unless explicitly approved.

## Local validation
Use `uv sync --frozen --extra dev` and run:
```bash
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -q
```
GitHub Actions uses Python 3.12. If PostgreSQL integration is unavailable, report that it
was skipped; never claim it was tested.

## Statistical and market-safety invariants
- Match event, canonical participants, period, market type and line and *audited settlement
  rules* exactly; unknown or mismatched fields must fail closed.
- Support exhaustive 2-way or 3-way outcomes only under explicit valid rules. Do not
  silently mix push, quarter-line, void, exchange-lay or overtime variants.
- No look-ahead. Use received/observed timestamps and only data available at decision time;
  older delayed snapshots must not override newer observations.
- +EV uses *estimated* fair probabilities; devig or consensus is not ground truth.
  A 2 percentage-point haircut is a scenario assumption, not a statistical confidence bound.
- Account for market fees, stake rounding, fill fraction, latency, price changes and
  unreliable liquidity. An apparent arbitrage is not guaranteed executable profit.
- Evaluate only on chronological out-of-sample data. Track coverage of closing lines,
  Brier/log-loss/calibration, CLV, filled-vs-unfilled and realized paper returns.

## Provider adapters
- Prefer documented and permitted sources. Confirm book, region, market and endpoint
  coverage before claiming Stake or Parimatch support.
- Isolate provider-specific parsing from canonical models. Record source provenance and
  distinguish original book quotes from duplicate reseller feeds.
- Do not store API keys in logs, raw journals, fixtures, commits or PR descriptions.
  Use environment variables; mock external HTTP in CI.
- Correctly handle provider errors and quota limits without bypassing provider controls.

## Telegram alerting
- Follow issue #6 only after validated feeds (issue #5).
- Alerts are research-only and clearly label hypothetical opportunities.
- Use allowlisted chat IDs, secret environment variables, cooldown/deduplication, explicit
  freshness and provenance, timeout/retry policies and isolated sender failures.

## Definition of done
- Implemented code + negative-case tests + user-facing docs.
- Ruff, formatting and pytest pass in CI; give exact status and any skipped tests.
- Reviewable PR that cites its GitHub issue. Do not merge unreviewed changes to main.
