# Список багов и проблем

## Новые (17.04.2026)
- [ ] **OpenAI 429:** Quota exceeded. Эмбеддинги RAG не могут быть созданы. Бот обрабатывает ошибку без падения.
- [ ] **ApeX Refresh Error:** Периодические ошибки обновления ApeX.

## Исправленные (17.04.2026)
- [x] **Borrow Checker AttributeError:** Исправлено в `check_gate_borrow` (теперь проверяет тип `data` перед `.get()`).
- [x] **Borrow Checker KuCoin HTTP 400:** Исправлено переходом на v1 эндпоинт.
- [x] **Borrow Checker TypeError:** Исправлено сравнение `ts_diff_ms` (NoneType vs int) в `check_all_borrow`.
- [x] **Session Leaks:** Исправлено в `radar/oi_monitor.py` (теперь `ex.close()` вызывается в `finally`).
- [x] **Binance HTTP 400 Logging:** Уровень логирования понижен до DEBUG для ожидаемых 400 ошибок (отсутствие монеты).
- [x] **Gate Ramp Radar HTTP 400:** Исправлено уменьшением `limit` до 100 в запросах к `/contracts`.

## Известные ограничения
- [ ] **Binance API:** Некоторые мелкие монеты с Gate отсутствуют на Binance (ожидаемое поведение).
