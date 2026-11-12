"""Изолированный сервер тестовой Mini App с Telegram и тестовой БД."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
from dotenv import dotenv_values
from sqlalchemy.ext.asyncio import create_async_engine

from config_data.config import load_config, validate_test_database

from .engine import GameError, LocalGameEngine
from .reminders import parse_reminders_flag, reminder_service
from .storage import WordliStorage
from .telegram_auth import MAX_TELEGRAM_USER_ID, validate_init_data

ROOT = Path(__file__).resolve().parent
MAX_BODY_BYTES = 32768
STATIC_ASSETS = {
    "/": ("index.html", "text/html"),
    "/index.html": ("index.html", "text/html"),
    "/styles.css": ("styles.css", "text/css"),
    "/prototype.js": ("prototype.js", "text/javascript"),
    "/telegram_client.js": ("telegram_client.js", "text/javascript"),
    "/assets/score-formula.png": ("assets/score-formula.png", "image/png"),
    "/assets/quality-formula.png": ("assets/quality-formula.png", "image/png"),
}
API_FIELDS = {
    "/api/state": set(),
    "/api/settings": {"timezone", "reminder_enabled"},
    "/api/leaderboard": set(),
    "/api/guess": {"day", "index", "guess", "revision", "request_id"},
    "/api/next": {"day", "index", "revision", "request_id"},
    "/api/draft": {"day", "index", "draft", "revision"},
}
CLIENT_SCRIPT = '<script src="prototype.js" defer></script>'
TELEGRAM_SCRIPTS = (
    '<script src="https://telegram.org/js/telegram-web-app.js?64"></script>'
    "\n  <script>window.WORDLI_MODE='telegram';</script>"
    '\n  <script src="telegram_client.js" defer></script>\n  ' + CLIENT_SCRIPT
)
SAFE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _utc_now() -> datetime:
    """Возвращает время сервера без часов клиентского устройства."""
    return datetime.now(timezone.utc)


def _origin(value: str) -> str:
    """Принимает только полный HTTPS origin без пути и секретов."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Нужен HTTPS origin игры")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or "\\" in value
            or re.search(r"[\x00-\x20\x7f]", value)
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError("Invalid origin")
    except ValueError:
        raise ValueError("Нужен HTTPS origin игры") from None
    return f"https://{parsed.netloc.lower()}"


def _json_response(payload: dict[str, Any], status: int = 200) -> web.Response:
    """Формирует JSON без кеширования и разрешений CORS."""
    return web.Response(
        text=json.dumps(payload, ensure_ascii=False, allow_nan=False),
        status=status,
        content_type="application/json",
        charset="utf-8",
        headers=SAFE_HEADERS,
    )


def _error_response(error: GameError) -> web.Response:
    """Возвращает только безопасный код и описание игровой ошибки."""
    return _json_response(
        {"error": {"code": error.code, "message": error.message}},
        error.status,
    )


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Запрещает повторные ключи в клиентском JSON-запросе."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


async def _read_payload(request: web.Request) -> dict[str, Any]:
    """Ограничивает размер и строго разбирает JSON-объект хода."""
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise GameError("request_too_large", "Запрос слишком большой.", 413)
    if request.content_type != "application/json":
        raise GameError("invalid_content_type", "Ожидается JSON-запрос.", 415)
    try:
        raw = await request.read()
        if len(raw) > MAX_BODY_BYTES:
            raise GameError("request_too_large", "Запрос слишком большой.", 413)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json)
    except GameError:
        raise
    except web.HTTPRequestEntityTooLarge:
        raise GameError("request_too_large", "Запрос слишком большой.", 413) from None
    except (ValueError, UnicodeError, RecursionError):
        raise GameError("invalid_json", "Некорректный JSON-запрос.", 400) from None
    if not isinstance(payload, dict):
        raise GameError("invalid_request", "Ожидается объект запроса.", 400)
    expected = API_FIELDS[request.path]
    if (
        request.path == "/api/settings"
        and not set(payload).issubset(expected)
        or request.path != "/api/settings"
        and set(payload) != expected
    ):
        raise GameError("invalid_request", "Некорректные поля запроса.", 400)
    return payload


def _telegram_display_name(init_data: str) -> str:
    """Читает имя только после успешной проверки всей подписи initData."""
    user = json.loads(dict(parse_qsl(init_data))["user"])
    parts = [user.get("first_name"), user.get("last_name")]
    return " ".join(part for part in parts if isinstance(part, str)) or "Игрок"


def create_app(
    storage: WordliStorage,
    bot_token: str,
    public_origin: str,
    allowed_user_ids: set[int] | None,
    clock: Callable[[], datetime] | None = None,
    expected_bot_id: int | None = None,
    *,
    mode: str = "test",
    readiness_check: Callable[[], Awaitable[bool]] | None = None,
) -> web.Application:
    """Создаёт игру с проверкой Telegram и явно выбранным режимом доступа."""
    if mode not in {"test", "production"}:
        raise ValueError("Неизвестный режим игрового сервера")
    production = mode == "production"
    if production and readiness_check is None:
        raise ValueError("Рабочий сервер требует проверки доступности БД")
    origin = _origin(public_origin)
    if (
        type(expected_bot_id) is not int
        or expected_bot_id <= 0
        or not isinstance(bot_token, str)
        or not re.fullmatch(r"[0-9]+:[^\s:]+", bot_token)
        or int(bot_token.partition(":")[0]) != expected_bot_id
    ):
        message = (
            "Не подтверждён идентификатор рабочего бота"
            if production
            else "Не подтверждён идентификатор тестового бота"
        )
        raise ValueError(message)
    if not (production and allowed_user_ids is None) and (
        not isinstance(allowed_user_ids, set)
        or not allowed_user_ids
        or any(
            type(user_id) is not int or not 0 < user_id <= MAX_TELEGRAM_USER_ID
            for user_id in allowed_user_ids
        )
    ):
        raise ValueError("Нужен непустой список пользователей тестовой игры")
    allowed = None if allowed_user_ids is None else frozenset(allowed_user_ids)
    get_time = clock or _utc_now
    public_host = urlsplit(origin).netloc

    def response_state(state: dict[str, Any]) -> dict[str, Any]:
        """Не выдаёт историю и помечает серверное хранение состояния."""
        now = get_time().astimezone(timezone.utc)
        return {
            **{key: value for key, value in state.items() if key != "history"},
            "server_time": now.isoformat().replace("+00:00", "Z"),
            "storage": "server",
        }

    def validate_request(request: web.Request) -> None:
        """Не принимает посторонние Host и межсайтовые API-запросы."""
        hosts = request.headers.getall("Host", [])
        socket = request.transport.get_extra_info("sockname")
        port = socket[1] if socket else None
        accepted_hosts = {public_host}
        if port is not None:
            accepted_hosts.update({f"127.0.0.1:{port}", f"localhost:{port}"})
        if len(hosts) != 1 or hosts[0].lower() not in accepted_hosts:
            raise GameError("invalid_host", "Адрес запроса запрещён.", 403)
        origins = request.headers.getall("Origin", [])
        if request.path.startswith("/api/") and (
            len(origins) > 1 or origins and origins[0] != origin
        ):
            raise GameError("invalid_origin", "Запрос с другого сайта запрещён.", 403)

    async def api_response(request: web.Request) -> web.Response:
        """Проверяет личность до обращения к истории в тестовой БД."""
        user_id: int | None = None
        try:
            tokens = request.headers.getall("X-Telegram-Init-Data", [])
            if len(tokens) != 1:
                raise GameError(
                    "authentication_failed",
                    "Откройте игру заново через кнопку «5БУКВ» бота."
                    if production
                    else "Откройте игру заново через тестового бота.",
                    401,
                )
            identity = validate_init_data(tokens[0], bot_token, now=get_time())
            if allowed is not None and identity.user_id not in allowed:
                raise GameError("access_denied", "Игра пока недоступна.", 403)
            user_id = identity.user_id
            payload = await _read_payload(request)
            if request.path == "/api/state":
                state = await storage.bootstrap(user_id)
                state["settings"] = await storage.settings(
                    user_id, display_name=_telegram_display_name(tokens[0])
                )
            elif request.path == "/api/settings":
                if (
                    "timezone" in payload
                    and not isinstance(payload["timezone"], str)
                    or "reminder_enabled" in payload
                    and type(payload["reminder_enabled"]) is not bool
                ):
                    raise GameError("invalid_settings", "Некорректные настройки.")
                state = {
                    "settings": await storage.settings(
                        user_id,
                        display_name=_telegram_display_name(tokens[0]),
                        timezone_name=payload.get("timezone"),
                        reminder_enabled=payload.get("reminder_enabled"),
                    )
                }
            elif request.path == "/api/leaderboard":
                await storage.settings(
                    user_id, display_name=_telegram_display_name(tokens[0])
                )
                state = {"leaderboard": await storage.leaderboard(user_id)}
            elif request.path == "/api/guess":
                state = await storage.submit(user_id, **payload)
            elif request.path == "/api/next":
                state = await storage.next_game(user_id, **payload)
            else:
                state = await storage.set_draft(user_id, **payload)
            return _json_response(response_state(state))
        except GameError as error:
            if production and error.code == "authentication_failed":
                error = GameError(
                    error.code,
                    "Откройте игру заново через кнопку «5БУКВ» бота.",
                    error.status,
                )
            if (
                user_id is not None
                and error.status == 409
                and error.code in {"version_conflict", "game_changed", "day_changed"}
            ):
                try:
                    current = await storage.bootstrap(user_id)
                    current["settings"] = await storage.settings(
                        user_id, display_name=_telegram_display_name(tokens[0])
                    )
                    state = response_state(current)
                    return _json_response(
                        {
                            "error": {
                                "code": error.code,
                                "message": error.message,
                            },
                            "state": state,
                        },
                        error.status,
                    )
                except GameError:
                    pass
            return _error_response(error)

    async def handle(request: web.Request) -> web.Response:
        """Отдаёт только игровые файлы и ограниченное тестовое API."""
        try:
            validate_request(request)
            if request.method in {"GET", "HEAD"}:
                if request.path == "/api/health":
                    ready = True
                    if production:
                        try:
                            ready = await readiness_check() is True
                        except Exception:
                            ready = False
                    return _json_response(
                        {
                            "ok": ready,
                            "test_only": not production,
                            "database": ready,
                            **({"mode": "production"} if production else {}),
                        },
                        200 if ready else 503,
                    )
                asset = STATIC_ASSETS.get(request.path)
                if asset is None:
                    raise GameError("not_found", "Страница не найдена.", 404)
                filename, content_type = asset
                content = (ROOT / filename).read_bytes()
                if filename == "index.html":
                    text = content.decode("utf-8")
                    if text.count(CLIENT_SCRIPT) != 1:
                        raise ValueError("Missing client entrypoint")
                    content = text.replace(CLIENT_SCRIPT, TELEGRAM_SCRIPTS).encode(
                        "utf-8"
                    )
                return web.Response(
                    body=content,
                    content_type=content_type,
                    charset="utf-8",
                    headers=SAFE_HEADERS,
                )
            if request.method == "POST":
                if request.path not in API_FIELDS:
                    raise GameError("not_found", "Запрос не найден.", 404)
                return await api_response(request)
            raise GameError("method_not_allowed", "Метод не поддерживается.", 405)
        except GameError as error:
            return _error_response(error)
        except Exception:
            return _error_response(
                GameError("internal_error", "Ошибка игрового сервера.", 500)
            )

    app = web.Application(client_max_size=MAX_BODY_BYTES + 1)
    app.router.add_route("*", "/{path:.*}", handle)
    return app


def main() -> None:
    """Запускает только loopback-сервер с явно тестовой конфигурацией."""
    parser = argparse.ArgumentParser(description="Тестовая Telegram-игра")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--env-file", default=".env.test.local")
    args = parser.parse_args()
    if os.getenv("APP_ENV", "test").lower() != "test":
        parser.error("Сервер игры разрешён только при APP_ENV=test")
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть в пределах 1–65535")
    values = dict(dotenv_values(args.env_file))
    values.update(os.environ)
    try:
        reminders_enabled = parse_reminders_flag(
            values.get("WORDLI_REMINDERS_ENABLED", "false")
        )
    except ValueError as error:
        parser.error(str(error))
    config = load_config(args.env_file)
    if not config.test_mode:
        parser.error("Сервер игры разрешён только в тестовом режиме")
    validate_test_database(config.database_url)
    database = create_async_engine(config.database_url)
    storage = WordliStorage(database, LocalGameEngine())
    app = create_app(
        storage,
        config.tg_bot.token_test,
        args.public_origin,
        set(config.tg_bot.owner) | set(config.tg_bot.admin_list),
        expected_bot_id=config.expected_test_bot_id,
    )

    async def resources(application: web.Application) -> AsyncIterator[None]:
        """Создаёт только таблицы игры и закрывает свой пул соединений."""
        try:
            await storage.init_schema()
            if reminders_enabled:
                async with reminder_service(
                    storage,
                    config.tg_bot.token_test,
                    public_origin=args.public_origin,
                    allowed_user_ids=(
                        set(config.tg_bot.owner) | set(config.tg_bot.admin_list)
                    ),
                ):
                    yield
            else:
                yield
        finally:
            await database.dispose()

    app.cleanup_ctx.append(resources)
    web.run_app(
        app,
        host="127.0.0.1",
        port=args.port,
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
