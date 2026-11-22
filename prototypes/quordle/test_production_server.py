"""Проверяет рабочую конфигурацию КВОРДЛИ без PostgreSQL и Telegram."""

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from . import production_server as prod
from .server import create_app
from .test_integration import signed_data


def environment() -> dict[str, str]:
    """Возвращает вымышленные реквизиты, не обращаясь к окружению."""
    return {
        "APP_ENV": "production",
        "QUORDLE_PUBLIC_ORIGIN": "https://games.example.org",
        "QUORDLE_DATABASE_URL": (
            "postgresql://quordle_runtime:fake_password@postgres:5432/copilot_quordle"
        ),
        "QUORDLE_EXPECTED_BOT_ID": "1234",
        "QUORDLE_EXPECTED_BOT_USERNAME": "@sample_game_bot",
        "BOT_TOKEN": "1234:dummy_token",
    }


class ConfigTest(unittest.TestCase):
    """Проверяет защитные ограничения рабочего контура."""

    def test_dedicated_runtime(self) -> None:
        """Без параметров рассылка выключена, ссылка ведёт в подкаталог."""
        env = environment()
        config = prod.load_config(env)
        self.assertFalse(config.reminders_enabled)
        self.assertEqual(config.port, 8795)
        self.assertEqual(config.webapp_url, config.origin + "/quordle/")
        self.assertNotIn("dummy_token", repr(config))
        self.assertNotIn("fake_password", repr(config))
        enabled = prod.load_config(env | {
            "QUORDLE_REMINDERS_ENABLED": "true",
            "QUORDLE_WEBAPP_URL": config.origin + "/games/quordle/",
        })
        self.assertTrue(enabled.reminders_enabled)
        self.assertEqual(enabled.webapp_url, config.origin + "/games/quordle/")

    def test_invalid_runtime(self) -> None:
        """Не разрешает тестовую БД, подмену бота и сторонний URL."""
        env = environment()
        dsn = env["QUORDLE_DATABASE_URL"]
        for key, value in (
            ("APP_ENV", "test"),
            ("QUORDLE_PUBLIC_ORIGIN", "https://foo.trycloudflare.com"),
            ("QUORDLE_PUBLIC_ORIGIN", "http://games.example.org"),
            ("QUORDLE_PUBLIC_ORIGIN", "https://games.example.org/quordle/"),
            ("QUORDLE_WEBAPP_URL", "https://evil.example.org/quordle/"),
            ("QUORDLE_WEBAPP_URL", "https://games.example.org/../wordli"),
            ("QUORDLE_DATABASE_URL", dsn + "_test"),
            ("QUORDLE_DATABASE_URL", dsn + "?dbname=copilot"),
            ("QUORDLE_DATABASE_URL", dsn.replace("quordle_runtime", "postgres")),
            ("QUORDLE_EXPECTED_BOT_ID", "1235"),
            ("QUORDLE_EXPECTED_BOT_USERNAME", "@TG_Template_Test_Bot"),
            ("QUORDLE_EXPECTED_BOT_USERNAME", ""),
            ("BOT_TOKEN", ""),
            ("QUORDLE_REMINDERS_ENABLED", "yes"),
            ("QUORDLE_PORT", "0"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                prod.load_config(env | {key: value})

    def test_production_excludes_preview_and_allowlist(self) -> None:
        """Рабочий API не принимает локальный просмотр или тестовый список."""
        for extra in ({"preview": True}, {"allowed": set()}, {"token": ""}):
            kwargs = {"token": "test-token", "production": True} | extra
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                create_app(AsyncMock(), MagicMock(), origin="https://example.org", **kwargs)


class RuntimeTest(unittest.IsolatedAsyncioTestCase):
    """Проверяет API, допуск и жизненный цикл с искусственными ресурсами."""

    async def test_get_me_identity(self) -> None:
        """Сверяет тип аккаунта, ID и фактическое имя настоящего бота."""
        config = prod.load_config(environment())
        bot = AsyncMock()
        for identifier, username, is_bot, valid in (
            (1234, "SAMPLE_GAME_BOT", True, True),
            (1234, "tg_template_test_bot", True, False),
            (1234, None, True, False),
            (1234, "sample_game_bot", False, False),
            (1235, "sample_game_bot", True, False),
        ):
            with self.subTest(username=username, identifier=identifier, is_bot=is_bot):
                bot.get_me.return_value = SimpleNamespace(
                    id=identifier, username=username, is_bot=is_bot,
                )
                if valid:
                    await prod.verify_bot(bot, config)
                else:
                    with self.assertRaises(RuntimeError):
                        await prod.verify_bot(bot, config)
                bot.get_me.assert_awaited_with(request_timeout=10)

    async def test_schema_without_ddl(self) -> None:
        """Каталоги только читаются, несовпадения блокируют запуск."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        columns = [{"column_name": k, "data_type": v} for k, v in prod.COLUMNS.items()]
        for db, rights, schema, checks, valid in (
            (prod.DATABASE, True, columns, True, True),
            ("copilot_quordle_test", True, columns, True, False),
            (prod.DATABASE, False, columns, True, False),
            (prod.DATABASE, True, columns[:-1], True, False),
            (prod.DATABASE, True, columns, False, False),
        ):
            with self.subTest(db=db, rights=rights, checks=checks, count=len(schema)):
                conn.fetchval.side_effect = [db, rights, checks]
                conn.fetch.return_value = schema
                if valid:
                    await prod.check_database(pool)
                else:
                    with self.assertRaises(RuntimeError):
                        await prod.check_database(pool)
        conn.execute.assert_not_awaited()
        queries = conn.fetchval.await_args_list + conn.fetch.await_args_list
        self.assertTrue(all(call.args[0].lstrip().startswith("select ") for call in queries))

    async def test_signed_public_api_and_db_health(self) -> None:
        """В рабочей игре нужен подлинный initData, но не список тестеров."""
        config = prod.load_config(environment())
        storage = AsyncMock()
        storage.operate.return_value = {"revision": 1}
        app = create_app(
            storage, MagicMock(), origin=config.origin,
            token=config.token, production=True,
        )
        headers = {"Host": "games.example.org", "Origin": config.origin}
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/state", json={}, headers=headers)
            self.assertEqual(response.status, 401)
            self.assertNotIn("тестов", (await response.json())["message"])
            for uid in (10, 99):
                signed = headers | {"X-Telegram-Init-Data": signed_data(uid, config.token)}
                response = await client.post("/api/state", json={}, headers=signed)
                self.assertEqual(response.status, 200)
                self.assertEqual(storage.operate.await_args.args[0].user_id, uid)
            response = await client.post(
                "/api/state", json={}, headers=signed | {"Origin": "https://other.org"}
            )
            self.assertEqual(response.status, 403)
            self.assertEqual((await client.get("/health")).status, 200)
            storage.pool.fetchval.assert_awaited_once_with(
                "select 1 from public.quordle_players limit 0", timeout=3
            )
            storage.pool.fetchval.side_effect = TimeoutError()
            self.assertEqual((await client.get("/health")).status, 503)

    async def test_startup_and_optional_worker(self) -> None:
        """Проверки обязательны, а отправитель запускается только явно."""
        config = prod.load_config(environment())
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                pool = MagicMock()
                bot_class = MagicMock()
                expected_bot = bot_class.return_value.__aenter__.return_value
                with (
                    patch.object(prod.asyncpg, "create_pool", return_value=pool),
                    patch.object(prod, "Bot", bot_class),
                    patch.object(prod, "check_database", new_callable=AsyncMock) as db,
                    patch.object(prod, "verify_bot", new_callable=AsyncMock) as verify,
                    patch.object(prod, "notify_players", new_callable=AsyncMock) as worker,
                ):
                    current = replace(config, reminders_enabled=enabled)
                    app = await prod.create_production_app(current)
                    runner = web.AppRunner(app)
                    await runner.setup()
                    try:
                        db.assert_awaited_once_with(pool)
                        verify.assert_awaited_once_with(expected_bot, current)
                        await asyncio.sleep(0)
                        if enabled:
                            self.assertEqual(worker.await_args.args[2], config.webapp_url)
                        else:
                            worker.assert_not_called()
                    finally:
                        await runner.cleanup()
                    pool.__aexit__.assert_awaited_once()
                    bot_class.return_value.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
