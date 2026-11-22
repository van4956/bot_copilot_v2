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

# В образе polling-бота нет серверов игр, тестовых стендов и баз.
COPY app.py ./
COPY common/ /app/common/
COPY config_data/ /app/config_data/
COPY database/*.py /app/database/
COPY filters/ /app/filters/
COPY handlers/ /app/handlers/
COPY locales/ /app/locales/
COPY middlewares/ /app/middlewares/
COPY prototypes/wordli/bot_texts.py /app/prototypes/wordli/

# Устанавливаем переменные окружения
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Проверяем основной entrypoint и ресурсы меню без бота, БД и сети.
RUN --network=none python -c "import app; from handlers import navigation; from pathlib import Path; assert Path('common/images/image_updates.jpg').is_file() and len(list(Path('locales').glob('*/LC_MESSAGES/*.mo'))) >= 4; assert not Path('local_testing').exists() and not Path('prototypes/quordle').exists()"

# Команда запуска бота
CMD ["python", "app.py"]
