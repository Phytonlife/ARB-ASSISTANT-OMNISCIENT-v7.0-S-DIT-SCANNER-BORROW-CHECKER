FROM python:3.11-slim

# Системные зависимости для FAISS + ccxt
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория
WORKDIR /app

# Зависимости (кэшируем отдельным слоем)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исходный код
COPY . .

# Создаём нужные директории
RUN mkdir -p data/strategies logs

# Non-root user для безопасности
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Запуск
CMD ["python", "-m", "bot.main"]
