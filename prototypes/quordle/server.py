"""Общий HTTP API КВОРДЛИ и отдельный запуск для локального тестового бота."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiohttp import web
from dotenv import dotenv_values

from prototypes.wordli.definitions import StaticDefinitionCatalog
from prototypes.wordli.engine import normalize_word
from prototypes.wordli.telegram_auth import validate_init_data

from .engine import GameError, QuordleEngine
from .storage import TEST_DATABASE, Player, RevisionConflict, Storage

STATIC_ROOT = Path(__file__).resolve().parent
ROOT = STATIC_ROOT.parents[1]
PG_FILE = ROOT / "local_testing/data/quordle-runtime/postgres.env"
ASSETS = {"index.html", "quordle.js", "quordle.css"}
TUNNEL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\Z")
LOGGER = logging.getLogger(__name__)
NOTICE = {
    "ru": ("Готова новая партия в КВОРДЛИ 🧩", "Играть"),
    "en": ("A new КВОРДЛИ puzzle is ready 🧩 (Russian words)", "Play"),
    "es": ("Nueva partida de КВОРДЛИ 🧩 (palabras en ruso)", "Jugar"),
    "uk": ("Нова партія КВОРДЛІ 🧩 (слова російською)", "Грати"),
}


def json_response(payload: dict[str, Any], status: int = 200) -> web.Response:
    """Возвращает JSON, который не кэшируется клиентом."""
    return web.json_response(
        payload, status=status, headers={"Cache-Control": "no-store"}
    )


def telegram_player(raw: str, token: str, allowed: set[int] | None) -> Player:
    """Читает публичное имя только после проверки подписи Telegram."""
    identity = validate_init_data(raw, token)
    if allowed is not None and identity.user_id not in allowed:
        raise GameError("forbidden", "Доступ к тестовой игре ограничен.", 403)
    user = json.loads(parse_qs(raw)["user"][0])
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join(part for part in parts if isinstance(part, str))
    name = " ".join(name.split())[:100] or "Игрок"
    locale = user.get("language_code", "ru")
    locale = locale.lower().replace("_", "-").split("-")[0] if isinstance(
        locale, str
    ) else "ru"
    return Player(identity.user_id, name, locale if locale in NOTICE else "ru")


async def notify_players(storage: Storage, bot: Bot, origin: str) -> None:
    """Отправляет только включённые сообщения в утреннее окно игрока."""
    keyboard = {
        locale: InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=label, web_app=WebAppInfo(url=origin))
        ]])
        for locale, (_, label) in NOTICE.items()
    }
    while True:
        try:
            now = datetime.now(timezone.utc)
            for player in await storage.claim_reminders(now):
                locale = player["locale"] if player["locale"] in NOTICE else "ru"
                try:
                    await bot.send_message(
                        player["user_id"],
                        NOTICE[locale][0],
                        reply_markup=keyboard[locale],
                    )
                except Exception as error:
                    LOGGER.warning("Напоминание не отправлено: %s", type(error).__name__)
        except Exception as error:
            LOGGER.warning("Напоминания недоступны: %s", type(error).__name__)
        await asyncio.sleep(60)


def create_app(
    storage: Storage,
    catalog: StaticDefinitionCatalog,
    *,
    origin: str,
    token: str = "",
    allowed: set[int] | None = None,
    preview: bool = False,
    production: bool = False,
) -> web.Application:
    """Собирает HTTP API с разрешённым origin и закрытым набором файлов."""
    host = urlsplit(origin).netloc
    if production and (preview or allowed is not None or not token):
        raise ValueError("Несовместимые настройки рабочего API КВОРДЛИ.")
    permitted = None if production else set(allowed or ())

    async def health(request: web.Request) -> web.Response:
        """Проверяет только выделенную БД."""
        try:
            if production:
                await storage.pool.fetchval(
                    "select 1 from public.quordle_players limit 0", timeout=3
                )
            else:
                await storage.pool.fetchval("select 1")
        except (OSError, asyncio.TimeoutError, asyncpg.PostgresError):
            return json_response({"ok": False}, 503)
        return json_response({"ok": True})

    async def static(request: web.Request) -> web.StreamResponse:
        """Отдаёт только именованные файлы интерфейса."""
        name = request.match_info.get("name", "") or "index.html"
        if name not in ASSETS:
            raise web.HTTPNotFound()
        headers = {
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' https://telegram.org; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; base-uri 'none'; object-src 'none'"
            ),
        }
        return web.FileResponse(STATIC_ROOT / name, headers=headers)

    async def api(request: web.Request) -> web.Response:
        """Проверяет источник и вызывает разрешённую игровую операцию."""
        action = request.match_info["action"]
        fields = {
            "state": set(),
            "draft": {"draft", "revision"},
            "guess": {"word", "revision", "request_id"},
            "next": {"revision", "request_id"},
            "settings": {"enabled", "timezone"},
            "rating": set(),
            "definition": {"word"},
        }
        if action not in fields:
            return json_response({"error": "not_found"}, 404)
        try:
            if request.host != host or request.headers.get("Origin", origin) != origin:
                raise GameError("forbidden", "Откройте игру из меню бота.", 403)
            if preview:
                if request.remote not in {"127.0.0.1", "::1"}:
                    raise GameError("forbidden", "Только локальный просмотр.", 403)
                player = Player(-1, "Локальный просмотр")
            else:
                values = request.headers.getall("X-Telegram-Init-Data", [])
                raw = values[0] if len(values) == 1 else ""
                player = telegram_player(raw, token, permitted)
            try:
                payload = await request.json()
            except (ValueError, TypeError, web.HTTPException, RecursionError):
                raise GameError("invalid_json", "Некорректный запрос.") from None
            if not isinstance(payload, dict) or payload.keys() - fields[action]:
                raise GameError("invalid_json", "Неизвестные поля запроса.")
            if "request_id" in payload and (
                not isinstance(payload["request_id"], str)
                or len(payload["request_id"]) > 36
            ):
                raise GameError("invalid_json", "Неверный идентификатор запроса.")
            if action == "settings":
                result = {"settings": await storage.update_settings(player, payload)}
            elif action == "rating":
                result = {"rating": await storage.rating(player.user_id)}
            elif action == "definition":
                word = normalize_word(payload.get("word"))
                if not await storage.get_definition_permission(player.user_id, word):
                    raise GameError(
                        "word_not_revealed",
                        "Определение доступно только для слов на экране результата.",
                        403,
                    )
                result = catalog.lookup(word)
                result["definition"] = result["definitions"][0]
            else:
                result = await storage.operate(player, action, payload)
            return json_response(result)
        except RevisionConflict as error:
            return json_response({
                "error": error.code, "message": error.message, "state": error.state,
            }, 409)
        except GameError as error:
            if production and error.code == "authentication_failed":
                return json_response({
                    "error": error.code, "message": "Откройте игру заново через бота.",
                }, error.status)
            return json_response({
                "error": error.code, "message": error.message,
            }, error.status)

    app = web.Application(client_max_size=8_192)
    app.router.add_get("/health", health)
    app.router.add_post("/api/{action}", api)
    app.router.add_get("/", static)
    app.router.add_get("/{name:index.html|quordle.js|quordle.css}", static)
    return app


async def initialise_connection(connection: asyncpg.Connection) -> None:
    """Настраивает преобразование JSONB без неявной сериализации."""
    await connection.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def run(origin: str, port: int, preview: bool) -> None:
    """Подключается только к локальной тестовой БД, затем поднимает HTTP."""
    from .demo_bot import load_test_identity, verify_test_bot

    pg = dotenv_values(PG_FILE, interpolate=False)
    if (
        pg.get("POSTGRES_DB") != TEST_DATABASE
        or pg.get("POSTGRES_USER") != "quordle_test"
        or not re.fullmatch(r"[a-f0-9]{64}", pg.get("POSTGRES_PASSWORD") or "")
    ):
        raise RuntimeError("Не настроена отдельная локальная БД КВОРДЛИ.")
    token, expected, allowed = ("", 0, set()) if preview else load_test_identity()
    async with asyncpg.create_pool(
        host="127.0.0.1",
        port=55492,
        database=TEST_DATABASE,
        user="quordle_test",
        password=pg["POSTGRES_PASSWORD"],
        init=initialise_connection,
        min_size=1,
        max_size=5,
        timeout=8,
        command_timeout=15,
    ) as pool:
        storage = Storage(pool, QuordleEngine())
        await storage.initialise_test_database()
        app = create_app(
            storage, StaticDefinitionCatalog(), origin=origin,
            token=token, allowed=allowed, preview=preview,
        )
        if not preview:
            async def telegram(_: web.Application) -> AsyncIterator[None]:
                """Проверяет личность бота и завершает обработчик уведомлений."""
                async with Bot(token=token) as bot:
                    await verify_test_bot(bot, expected)
                    worker = asyncio.create_task(notify_players(storage, bot, origin))
                    try:
                        yield
                    finally:
                        worker.cancel()
                        with suppress(asyncio.CancelledError):
                            await worker

            app.cleanup_ctx.append(telegram)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", port).start()
            LOGGER.info("КВОРДЛИ доступен на 127.0.0.1:%d.", port)
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


def main() -> int:
    """Проверяет параметры запуска, не записывает реквизиты в логи."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    if args.preview and (args.origin or args.port == 8792):
        parser.error("Локальный просмотр требует отдельный порт, например 8793.")
    if not args.preview and not TUNNEL.fullmatch(args.origin or ""):
        parser.error("Укажите HTTPS-адрес выделенного туннеля.")
    origin = f"http://127.0.0.1:{args.port}" if args.preview else args.origin
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(origin, args.port, args.preview))
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        LOGGER.error("Сервер КВОРДЛИ не запустился: %s.", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
