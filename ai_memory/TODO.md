# Список задач (TODO)

## Высокий приоритет (Исправление ошибок)
- [ ] **Восстановить функции в `radar/`**:
  - Добавить `format_oi_alert` в `radar/oi_monitor.py`.
  - Реализовать `ramp_scan` и `format_ramp_alert` в `radar/ramp_hunter.py`.
  - Добавить `format_margin_event` в `radar/margin_monitor.py`.
- [ ] **Исправить Docker Healthcheck**:
  - Обновить `docker-compose.yml`: `pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}`.
- [ ] **Отладка OpenAI Embeddings**:
  - Проверить валидность ключа в `.env`.
  - Добавить обработку ошибок в `oracle/rag_memory.py`.

## Средний приоритет (Улучшения)
- [ ] **Оптимизация CCXT**:
  - Добавить явный `await exchange.close()` во все сканеры (исправление ворнингов в логах).
- [ ] **Telegram UI**:
  - Исправить `PTBUserWarning` в `bot/ui.py`, установив `per_message=True` или `per_chat=True` в `ConversationHandler`.

## Низкий приоритет
- [ ] **Документация**:
  - Обновить `allmargin.md` и `s-dtmargin.md` в соответствии с актуальным кодом.
