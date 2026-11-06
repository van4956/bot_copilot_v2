"""Запускает только тестового бота с настроенной игрой Wordli."""

import asyncio
import logging
import os

from app import main as run_bot
from config_data.config import ROOT, load_config


def main() -> None:
    """Проверяет тестовый режим и не читает боевые настройки."""
    os.environ["APP_ENV"] = "test"
    os.environ["APP_ENV_FILE"] = str(ROOT / ".env.test.local")
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    expected = config.expected_test_bot_id
    if (
        not config.test_mode
        or expected is None
        or config.tg_bot.token_test.split(":", 1)[0] != str(expected)
        or not config.wordli_webapp_url
    ):
        raise ValueError("Не настроен подтверждённый тестовый бот Wordli")
    asyncio.run(run_bot(config))


if __name__ == "__main__":
    main()
