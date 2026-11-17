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
