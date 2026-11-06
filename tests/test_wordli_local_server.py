"""Проверки локального HTTP-сервера Wordli без ботов и баз данных."""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from email.message import Message
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from prototypes.wordli.engine import LocalGameEngine
from prototypes.wordli.local_server import (
    MAX_BODY_BYTES,
    LocalRequestHandler,
    LocalWordliServer,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def local_server(tmp_path: Path) -> Iterator[LocalWordliServer]:
    """Создаёт отдельный loopback-сервер с маленьким тестовым словарём."""
    words = sorted(["КОМАР", "КОШКА", "ТАЧКА", "ПАРОМ", "БИЛЕТ", "ЛАМПА"])
    filename = tmp_path / "ru5.json"
    filename.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dictionary_id": "local-http-test",
                "normalization": "Ё=Е",
                "words": words,
                "answers": words,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    engine = LocalGameEngine(filename)
    with LocalWordliServer(port=0, engine=engine, clock=lambda: NOW) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=3)


def request(
    server: LocalWordliServer,
    method: str,
    path: str,
    payload: dict[str, Any] | bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Обращается только к выделенному локальному тестовому порту."""
    request_headers = dict(headers or {})
    if isinstance(payload, dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    else:
        body = payload
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_bootstrap_and_health_are_local_only(
    local_server: LocalWordliServer,
) -> None:
    """Пустая игра не раскрывает ответ и не использует БД."""
    status, headers, raw = request(local_server, "POST", "/api/state", {})
    result = json.loads(raw)
    assert status == 200
    assert result["day"] == "2026-11-05"
    assert result["server_time"] == "2026-11-05T12:00:00Z"
    assert result["game"]["answer"] is None
    assert result["game"]["rows"] == []
    assert result["stats"]["played"] == 0
    assert headers["Cache-Control"] == "no-store"
    assert "Access-Control-Allow-Origin" not in headers

    status, _, raw = request(local_server, "GET", "/api/health")
    assert status == 200
    assert json.loads(raw) == {
        "ok": True,
        "local_only": True,
        "database": False,
    }


def test_winning_request_and_reload_do_not_duplicate_statistics(
    local_server: LocalWordliServer,
) -> None:
    """Повтор отправки той же истории не создаёт две победы."""
    answer = local_server.engine.word_for_day(NOW.date())
    payload = {"history": [], "day": "2026-11-05", "guess": answer}
    first = request(local_server, "POST", "/api/guess", payload)
    second = request(local_server, "POST", "/api/guess", payload)
    assert first[0] == second[0] == 200
    result = json.loads(first[2])
    assert result == json.loads(second[2])
    assert result["game"]["outcome"] == "win"
    assert result["game"]["answer"] == answer
    assert result["stats"]["played"] == result["stats"]["wins"] == 1
    status, _, raw = request(
        local_server, "POST", "/api/state", {"history": result["history"]}
    )
    assert status == 200
    assert json.loads(raw)["stats"]["wins"] == 1


def test_expired_request_returns_new_day_without_a_loss(
    local_server: LocalWordliServer,
) -> None:
    """Ход старого дня не переносится в новую партию."""
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    status, _, raw = request(
        local_server,
        "POST",
        "/api/guess",
        {
            "history": [{"day": yesterday, "guesses": [], "draft": "КО"}],
            "day": yesterday,
            "guess": "КОМАР",
        },
    )
    result = json.loads(raw)
    assert status == 409
    assert result["error"]["code"] == "day_changed"
    assert result["state"]["day"] == "2026-11-05"
    assert result["state"]["game"]["rows"] == []
    assert result["state"]["stats"]["played"] == 0


@pytest.mark.parametrize(
    "path",
    [
        "/.env",
        "/../../.env",
        "/%2e%2e/%2e%2e/.env",
        "/app.py",
        "/engine.py",
        "/local_server.py",
        "/data/ru5.json",
        "/data/archive/ru5-opencorpora-wordfreq-v1-165050f1e2b8.json",
        "/README.md",
        "/data/",
    ],
)
def test_private_files_are_not_served(
    local_server: LocalWordliServer, path: str
) -> None:
    """Белый список ресурсов не раскрывает словарь и файлы проекта."""
    status, _, _ = request(local_server, "GET", path)
    assert status == 404


def test_html_and_head_are_available(local_server: LocalWordliServer) -> None:
    """Интерфейс обслуживается без выдачи каталогов."""
    status, headers, raw = request(local_server, "GET", "/")
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert "5БУКВ" in raw.decode("utf-8")
    status, headers, raw = request(local_server, "HEAD", "/styles.css")
    assert status == 200
    assert int(headers["Content-Length"]) > 0
    assert raw == b""


@pytest.mark.parametrize("origin", ["https://example.com", "null"])
def test_other_origins_are_rejected_in_handler(
    local_server: LocalWordliServer,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    """Проверяет запрет Origin без нестабильного транспортного обрыва."""
    handler = object.__new__(LocalRequestHandler)
    handler.server = local_server
    handler.path = "/api/state"
    handler.headers = Message()
    handler.headers["Host"] = f"127.0.0.1:{local_server.server_port}"
    handler.headers["Origin"] = origin
    errors = []
    monkeypatch.setattr(handler, "_error", lambda *args: errors.append(args))
    assert handler._validate_request() is False
    assert errors == [
        (403, "invalid_origin", "Запрос с другого сайта запрещён.")
    ]


def test_invalid_host_is_rejected_without_network_filters(
    local_server: LocalWordliServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверяет Host отдельно от системной фильтрации HTTP-трафика."""
    handler = object.__new__(LocalRequestHandler)
    handler.server = local_server
    handler.path = "/api/state"
    handler.headers = Message()
    handler.headers["Host"] = "example.com"
    errors = []
    monkeypatch.setattr(handler, "_error", lambda *args: errors.append(args))
    assert handler._validate_request() is False
    assert errors[0][0:2] == (403, "invalid_host")


@pytest.mark.parametrize("body", [b"[]", b"null", b"{", b"\xff"])
def test_invalid_json_is_rejected(
    local_server: LocalWordliServer, body: bytes
) -> None:
    """Ошибочное тело запроса не запускает обработку хода."""
    status, _, raw = request(
        local_server,
        "POST",
        "/api/state",
        body,
        headers={"Content-Type": "application/json"},
    )
    assert status == 400
    assert "error" in json.loads(raw)


def test_oversized_request_is_rejected_before_reading(
    local_server: LocalWordliServer,
) -> None:
    """Слишком большой запрос отклоняется без чтения всей истории."""
    status, _, _ = request(
        local_server,
        "POST",
        "/api/state",
        b"",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(MAX_BODY_BYTES + 1),
        },
    )
    assert status == 413


def test_next_route_opens_another_word_and_counts_real_results(
    local_server: LocalWordliServer,
) -> None:
    """После победы API открывает второе слово и сохраняет первую партию."""
    day = NOW.date().isoformat()
    status, _, raw = request(
        local_server,
        "POST",
        "/api/guess",
        {
            "history": [],
            "day": day,
            "guess": local_server.engine.word_for_day(NOW.date()),
        },
    )
    assert status == 200
    won = json.loads(raw)
    assert won["game"]["index"] == 0
    assert won["progression"]["total_wins"] == 1
    status, _, raw = request(
        local_server,
        "POST",
        "/api/next",
        {"history": won["history"], "day": day, "index": 0},
    )
    state = json.loads(raw)
    assert status == 200
    assert state["game"]["index"] == 1
    assert state["game"]["id"] == f"{day}:1"
    assert state["game"]["rows"] == []
    assert state["game"]["answer"] is None
    assert state["stats"]["wins"] == 1
    assert state["server_time"] == "2026-11-05T12:00:00Z"
    answer = local_server.engine.word_for_game(NOW.date(), 1)
    assert answer != won["game"]["answer"]
    status, _, raw = request(
        local_server,
        "POST",
        "/api/guess",
        {
            "history": state["history"],
            "day": day,
            "index": 1,
            "guess": answer,
        },
    )
    second = json.loads(raw)
    assert status == 200
    assert second["game"]["outcome"] == "win"
    assert second["stats"]["played"] == 2
    assert second["stats"]["strict_streak"] == 2
    assert second["progression"]["total_wins"] == 2
    assert second["progression"]["nodes"][2] == {
        "number": 3,
        "word": None,
        "complete": False,
    }


def test_next_route_cannot_skip_unfinished_game(
    local_server: LocalWordliServer,
) -> None:
    """Нельзя открыть следующее слово, не завершив текущую партию."""
    status, _, raw = request(
        local_server,
        "POST",
        "/api/next",
        {"history": [], "day": NOW.date().isoformat(), "index": 0},
    )
    assert status == 409
    assert json.loads(raw)["error"]["code"] == "game_in_progress"


@pytest.mark.parametrize("route", ["/api/guess", "/api/next"])
def test_stale_index_returns_current_game_for_resynchronization(
    local_server: LocalWordliServer, route: str
) -> None:
    """Старая вкладка получает актуальную партию без списания её попытки."""
    day = NOW.date().isoformat()
    state = local_server.engine.next_game(
        [
            {
                "day": day,
                "guesses": [local_server.engine.word_for_day(NOW.date())],
            }
        ],
        day,
        0,
        NOW.date(),
    )
    status, _, raw = request(
        local_server,
        "POST",
        route,
        {
            "history": state["history"],
            "day": day,
            "index": 0,
            "guess": "КОШКА",
        },
    )
    result = json.loads(raw)
    assert status == 409
    assert result["error"]["code"] == "game_changed"
    assert result["state"]["game"]["index"] == 1
    assert result["state"]["game"]["rows"] == []
    assert result["state"]["stats"]["wins"] == 1
    assert result["state"]["server_time"] == "2026-11-05T12:00:00Z"


def test_repeated_next_payload_has_identical_response(
    local_server: LocalWordliServer,
) -> None:
    """Двойное нажатие не пропускает сразу два слова."""
    day = NOW.date().isoformat()
    payload = {
        "history": [
            {
                "day": day,
                "guesses": [local_server.engine.word_for_day(NOW.date())],
            }
        ],
        "day": day,
        "index": 0,
    }
    first = request(local_server, "POST", "/api/next", payload)
    second = request(local_server, "POST", "/api/next", payload)
    assert first[0] == second[0] == 200
    assert json.loads(first[2]) == json.loads(second[2])
    assert json.loads(first[2])["game"]["index"] == 1


def test_next_route_rejects_old_day_without_carrying_move(
    local_server: LocalWordliServer,
) -> None:
    """Запрос продолжения вчерашнего дня не открывает допигру сегодня."""
    yesterday = (NOW - timedelta(days=1)).date()
    status, _, raw = request(
        local_server,
        "POST",
        "/api/next",
        {
            "history": [
                {
                    "day": yesterday.isoformat(),
                    "guesses": [local_server.engine.word_for_day(yesterday)],
                }
            ],
            "day": yesterday.isoformat(),
            "index": 0,
        },
    )
    result = json.loads(raw)
    assert status == 409
    assert result["error"]["code"] == "day_changed"
    assert result["state"]["game"]["day"] == NOW.date().isoformat()
    assert result["state"]["game"]["index"] == 0
    assert result["state"]["game"]["rows"] == []
    assert result["state"]["stats"]["played"] == 1


@pytest.mark.parametrize("index", [True, None, -1, "1", 1000])
def test_invalid_indexes_fail_without_internal_error(
    local_server: LocalWordliServer, index: Any
) -> None:
    """Повреждённый номер партии отклоняется типизированной ошибкой."""
    status, _, raw = request(
        local_server,
        "POST",
        "/api/next",
        {"history": [], "day": NOW.date().isoformat(), "index": index},
    )
    assert status == 400
    assert json.loads(raw)["error"]["code"] == "invalid_index"


def test_local_expired_words_never_repeat_and_exhaustion_keeps_stats(
    local_server: LocalWordliServer,
) -> None:
    """Даже пустые просроченные партии расходуют пул без поражений."""
    history: list[dict[str, Any]] = []
    issued: set[str] = set()
    for offset in range(6):
        now = NOW + timedelta(days=offset)
        local_server.clock = lambda value=now: value
        status, _, raw = request(
            local_server, "POST", "/api/state", {"history": history}
        )
        current = json.loads(raw)
        assert status == 200
        history = current["history"]
        answer = history[-1]["answer"]
        assert answer not in issued
        issued.add(answer)
        assert current["game"]["answer"] is None
        assert current["game"]["status"] == "in_progress"
        assert current["availability"]["remaining"] == 5 - offset
        assert current["stats"]["played"] == current["stats"]["wins"] == 0
        repeated = request(
            local_server, "POST", "/api/state", {"history": history}
        )
        assert repeated[0] == 200
        assert json.loads(repeated[2]) == current
    local_server.clock = lambda: NOW + timedelta(days=6)
    status, _, raw = request(
        local_server, "POST", "/api/state", {"history": history}
    )
    exhausted = json.loads(raw)
    assert status == 200
    assert exhausted["game"]["status"] == "exhausted"
    assert exhausted["game"]["answer"] is None
    assert exhausted["history"] == history
    assert exhausted["availability"] == {"remaining": 0, "exhausted": True}
    assert exhausted["stats"]["played"] == exhausted["stats"]["wins"] == 0
    status, _, raw = request(
        local_server,
        "POST",
        "/api/guess",
        {"history": history, "day": exhausted["day"], "guess": "КОШКА"},
    )
    assert status == 409
    assert json.loads(raw)["error"]["code"] == "no_more_words"
