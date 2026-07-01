# Архитектура проекта ARB Assistant

Проект представляет собой модульного торгового бота для арбитража и анализа рыночных аномалий (фандинг, OI, рампы, индексные отклонения).

## Структура папок

- `bot/`: Логика Telegram бота.
  - `handlers/`: Обработчики команд (admin, analyze, dex, funding и т.д.).
  - `main.py`: Точка входа бота.
  - `ui.py`: Клавиатуры и форматирование сообщений.
- `core/`: Ядро системы.
  - `config.py`: Настройки через `pydantic-settings`.
  - `database.py`: Работа с БД (PostgreSQL/SQLAlchemy).
  - `redis_cache.py`: Кэширование через Redis.
- `data/`: Статические данные и конфигурации стратегий.
  - `strategies/`: Текстовые файлы для RAG (Knowledge Base).
  - `exchanges.py`, `fees.py`: Справочники бирж и комиссий.
- `hunter/`: Математические движки и логика исполнения.
  - `math_engine.py`: Расчеты арбитража.
  - `risk_engine.py`: Управление рисками.
- `oracle/`: AI-модуль.
  - `groq_client.py`: Интеграция с LLM (Llama 3).
  - `rag_memory.py`: Векторная память (FAISS) для обучения бота стратегиям.
- `radar/`: Мониторинг рынка.
  - `scheduler.py`: Планировщик фоновых задач (APScheduler).
  - `oi_monitor.py`, `ramp_hunter.py`, `funding_monitor.py`: Различные сканеры.

## Технологический стек

- **Language:** Python 3.11
- **Database:** PostgreSQL 16 (v2), SQLite (v1)
- **Cache:** Redis 7
- **Libraries:**
  - `ccxt`: Работа с криптобиржами.
  - `python-telegram-bot`: Интерфейс пользователя.
  - `sqlalchemy` + `asyncpg`: Асинхронная работа с БД.
  - `apscheduler`: Фоновые задачи.
  - `faiss-cpu` + `openai`: Векторный поиск для RAG.
  - `loguru`: Логирование.
