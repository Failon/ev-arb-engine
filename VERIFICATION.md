# Проверка MVP — 17 ноября 2026

- Python 3.12.16: 49 passed, 1 skipped.
- Python 3.14.7: 49 passed, 1 skipped.
- Ruff check и format --check: успешно.
- CLI analyze с fixtures/quotes.json и записью в SQLite: успешно.
- CLI backtest с fixtures/walk_forward.json: успешно, два test fold.
- CLI replay с fixtures/replay.json: успешно, восемь синтетических событий.
- CLI report с fixtures/report.json: успешно.
- Исторические отчёты: weighted ROI, drawdown, одновременный settlement,
  повторяемость bootstrap, отсутствие данных, невалидные и будущие наблюдения.
- GET / и POST /reports проверены через ASGI; HTML-панель включена в пакет.
- Replay проверяет отсутствие влияния будущих цен/результатов на решение,
  частичное исполнение, изменение цены на arrival, void и отсутствие closing references.
- Старые и запоздавшие котировки не заменяют свежие в анализе, сканере и симуляции.
- FastAPI /health и /analyze проверены через ASGI transport без сети.
- The Odds API collector проверен через MockTransport: нормализация, журнал,
  отсутствие правил, HTTP 401/429/500/302, отсутствие автоматических повторов.
- Реальные запросы odds API и реальные ставки не выполнялись.
- PostgreSQL integration test пропущен: TEST_POSTGRES_DSN не настроен,
  Docker daemon недоступен. Поддержка PostgreSQL реализована, но в этой среде
  не подтверждена живым сервером.

Ограничение среды проверки: Windows asyncio требует локальный socketpair;
в sandbox создание loop зависает, вне sandbox офлайн тесты проходят за ~1 секунду.
Зависимости закреплены в uv.lock. Подробные ограничения моделей приведены в README.

## PR #2 follow-up: numeric boundary audit (2026-11-17)

Baseline 9944649: 53 passed, 1 skipped. Added unanimous positive/negative/zero EV,
repeated snapshots and late receipt regressions. The zero case exposed power-devig
root roundoff (0.5000000000000001 for symmetric references), incorrectly making
an exactly fair candidate positive. Normalize the solved probability vector by
its accurately summed mass; symmetric references now return exactly 0.5.
Validation: 58 passed, 1 skipped; Ruff lint and format passed. PostgreSQL remains
unverified (TEST_POSTGRES_DSN absent). No live provider or Telegram calls.

## PR #7 follow-up: provider validation

79 passed, 1 skipped; Ruff lint and formatting pass. Includes the #8 numeric fix,
audited event/bookmaker mapping, duplicated-reseller consensus regression, changed
metadata rejection, pre-match/rule validation and successful-response key echo
redaction. All HTTP is mocked; no live provider subscription or PostgreSQL tested.

## Autonomous scanner and paper ledger (#10)

88 passed, 1 skipped; Ruff lint and format passed. scan-demo CLI: 3 synthetic
signals, balance 1000, reserved 10, available 990. Tests cover source outages,
quota/backoff, Retry-After, post-collection cutoff, future/stale snapshots,
strict market grouping, two-book arb versus three-book EV, persistent reserves,
win/loss/void and idempotent/conflicting settlement. Real feeds remain unverified;
no real provider/Telegram requests, PostgreSQL skipped. Paper execution is an
instantaneous scenario; latency/CLV remain in the existing historical replay.

## Telegram synthetic demo (#6)

101 passed, 1 skipped; Ruff lint and formatting passed. CLI alerts-demo produced
3 local synthetic message previews without credentials or network. MockTransport
covers success, 401/500/302, transport errors, short/long 429 retry-after, freshness
on retry, filters, conflicting markets, modified EV, real-data refusal, allowed-chat
commands, polling cursor, persistent dedup, material changes, cooldown/hourly quota,
bounded queue and missing-secret errors. No actual Telegram message was sent.
Real-feed notifications remain disabled pending #5 authenticated data and licence
verification; this delivers the explicitly labelled synthetic demo path only.

## OddsPapi + The Odds API free live-data pilot (#13)

Added quota-bounded read-only HTTP, OddsPapi V4 parsing and a separate live trial
CLI reusing The Odds API normalization. 125 passed, 1 skipped; Ruff lint/format
passed. The complete synthetic 72-hour schedule used 84 OddsPapi requests and
72 The Odds API requests, with identical snapshots deduplicated. Tests include
budget/cooldown persistence, 401/403/429/302/500 and transport failure isolation,
request-credit headers, late/future/old outcome-change timestamps, unknown audits,
unsupported sports/player/exchange outcomes, duplicate original-book identities,
changed fixture metadata, safe local secret loading, offline reporting and frozen
trial deadlines/configuration. Metadata discovery caches and shares the same caps.

Local live-check: both API keys absent, network_requests=0. User has no provider
accounts yet. No authenticated live requests, three-day deployment, paper fills,
real wagers or Telegram sends were performed. Settlement/market/book edition audits
must be completed from actual discovery before EV signals are enabled; default
configuration only collects inventory. PostgreSQL remains skipped without DSN.
Public provider docs/terms were read; systemd template was not installed/tested.
