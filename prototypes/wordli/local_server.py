"""Локальный сервер игры без ботов, авторизации и подключения к БД."""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .engine import GameError, LocalGameEngine

ROOT = Path(__file__).resolve().parent
MAX_BODY_BYTES = 2 * 1024 * 1024
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/prototype.js": ("prototype.js", "text/javascript; charset=utf-8"),
    "/assets/score-formula.png": ("assets/score-formula.png", "image/png"),
    "/assets/quality-formula.png": ("assets/quality-formula.png", "image/png"),
}
logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Возвращает время локального сервера в UTC."""
    return datetime.now(timezone.utc)


class LocalRequestHandler(BaseHTTPRequestHandler):
    """Обслуживает только разрешённые файлы и локальные игровые запросы."""

    server: LocalWordliServer
    server_version = "WordliLocal/0.1"
    sys_version = ""

    def setup(self) -> None:
        """Ограничивает ожидание незавершённого локального запроса."""
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args: Any) -> None:
        """Не записывает локальную историю и ввод игрока в журналы."""

    def _send(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        content = json.dumps(
            payload, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self._send(status, content, "application/json; charset=utf-8")

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"error": {"code": code, "message": message}})

    def _validate_request(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        allowed_hosts = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        if len(hosts) != 1 or hosts[0].strip().lower() not in allowed_hosts:
            self._error(
                403, "invalid_host", "Доступ разрешён только локально."
            )
            return False
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{hosts[0].strip().lower()}":
            self._error(
                403, "invalid_origin", "Запрос с другого сайта запрещён."
            )
            return False
        try:
            target = urlsplit(self.path)
        except ValueError:
            self._error(400, "invalid_path", "Некорректный адрес запроса.")
            return False
        if target.scheme or target.netloc:
            self._error(400, "invalid_path", "Некорректный адрес запроса.")
            return False
        return True

    def _path(self) -> str:
        return unquote(urlsplit(self.path).path)

    def do_GET(self) -> None:
        """Отдаёт интерфейс, но не словарь, исходники Python или настройки."""
        if not self._validate_request():
            return
        path = self._path()
        if path == "/api/health":
            self._json(
                200, {"ok": True, "local_only": True, "database": False}
            )
            return
        asset = ASSETS.get(path)
        if asset is None:
            self._error(404, "not_found", "Страница не найдена.")
            return
        filename, content_type = asset
        try:
            content = (ROOT / filename).read_bytes()
        except OSError:
            self._error(
                500, "asset_unavailable", "Файл интерфейса недоступен."
            )
            return
        self._send(200, content, content_type)

    def do_HEAD(self) -> None:
        """Возвращает заголовки разрешённого ресурса без содержимого."""
        self.do_GET()

    def do_OPTIONS(self) -> None:
        """Не разрешает обращения к локальной игре с других сайтов."""
        if self._validate_request():
            self._error(405, "method_not_allowed", "Метод не поддерживается.")

    def _read_payload(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise GameError("invalid_request", "Некорректный запрос.", 400)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise GameError("invalid_length", "Не задан размер запроса.", 411)
        try:
            length = int(lengths[0])
        except ValueError as error:
            raise GameError(
                "invalid_length", "Некорректный размер запроса.", 400
            ) from error
        if length < 0:
            raise GameError(
                "invalid_length", "Некорректный размер запроса.", 400
            )
        if length > MAX_BODY_BYTES:
            raise GameError(
                "request_too_large", "Слишком большая история.", 413
            )
        if self.headers.get_content_type() != "application/json":
            raise GameError(
                "invalid_content_type", "Ожидается JSON-запрос.", 415
            )
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete body")
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise GameError(
                "invalid_json", "Некорректный JSON-запрос.", 400
            ) from error
        if not isinstance(payload, dict):
            raise GameError(
                "invalid_request", "Ожидается объект запроса.", 400
            )
        return payload

    def _with_time(
        self, state: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        return {**state, "server_time": now.isoformat().replace("+00:00", "Z")}

    def do_POST(self) -> None:
        """Проверяет ход и историю без серверного сохранения."""
        if not self._validate_request():
            return
        path = self._path()
        if path not in {"/api/state", "/api/guess", "/api/next"}:
            self._error(404, "not_found", "Запрос не найден.")
            return
        history: Any = []
        now = self.server.clock().astimezone(timezone.utc)
        try:
            payload = self._read_payload()
            history = payload.get("history", [])
            if path == "/api/state":
                state = self.server.engine.bootstrap(history, today=now.date())
            elif path == "/api/next":
                state = self.server.engine.next_game(
                    history,
                    payload.get("day"),
                    payload.get("index", 0),
                    today=now.date(),
                )
            else:
                state = self.server.engine.submit(
                    history,
                    payload.get("day"),
                    payload.get("guess"),
                    today=now.date(),
                    index=payload.get("index", 0),
                )
            self._json(200, self._with_time(state, now))
        except GameError as error:
            response: dict[str, Any] = {
                "error": {"code": error.code, "message": error.message}
            }
            if error.code in {"day_changed", "game_changed"}:
                try:
                    response["state"] = self._with_time(
                        self.server.engine.bootstrap(
                            history, today=now.date()
                        ),
                        now,
                    )
                except GameError:
                    pass
            self._json(error.status, response)
        except Exception as error:
            logger.error("Ошибка локальной игры: %s", type(error).__name__)
            self._error(
                500, "internal_error", "Ошибка локального игрового сервера."
            )


class LocalWordliServer(ThreadingHTTPServer):
    """Запускает игру только на IPv4 loopback без доступа к боевой среде."""

    daemon_threads = True

    def __init__(
        self,
        port: int = 8765,
        engine: LocalGameEngine | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Принимает движок и часы для изолированных тестов."""
        self.engine = engine if engine is not None else LocalGameEngine()
        self.clock = clock if clock is not None else _utc_now
        super().__init__(("127.0.0.1", port), LocalRequestHandler)


def main() -> None:
    """Открывает локальную игру, не импортируя и не запуская бота."""
    parser = argparse.ArgumentParser(
        description="Локальная игра «5БУКВ» без БД"
    )
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("Порт должен быть от 1 до 65535.")
    with LocalWordliServer(port=arguments.port) as server:
        print(
            f"Локальная игра: http://127.0.0.1:{server.server_port}/",
            flush=True,
        )
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
