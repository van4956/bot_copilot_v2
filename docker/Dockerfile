# Используем официальный Python образ на базе Alpine Linux для меньшего размера
FROM python:3.11-slim AS builder

# Устанавливаем необходимые зависимости для сборки
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию в контейнере
WORKDIR /app

# Копируем только файл с зависимостями
COPY requirements.txt .

# Устанавливаем зависимости в виртуальное окружение
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Второй этап для финального образа
FROM python:3.11-slim

# Копируем виртуальное окружение из builder
COPY --from=builder /opt/venv /opt/venv

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем исходный код проекта
COPY . .

# Устанавливаем переменные окружения
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Проверяем работоспособность установленных пакетов
RUN python -c "import aiogram"

# Команда запуска бота
CMD ["python", "app.py"]