"""Проверяет контракты API и атомарные ходы в выделенной тестовой БД."""

import asyncio
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode
from uuid import uuid4

import asyncpg
from aiohttp.test_utils import TestClient, TestServer
from dotenv import dotenv_values

from .engine import GameError, QuordleEngine
from .server import PG_FILE, create_app, initialise_connection
from .storage import TEST_DATABASE, Player, RevisionConflict, Storage


def signed_data(user_id: int, token: str) -> str:
    """Создаёт подписанный Telegram-запрос с искусственным профилем."""
    values = {
        "user": json.dumps({"id": user_id, "first_name": "Тест"}),
        "auth_date": str(int(time.time())),
    }
    message = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class ApiTest(unittest.IsolatedAsyncioTestCase):
    """Тестирует доступ без настоящего бота и данных пользователей."""

    async def test_authorisation_origin_and_word_rights(self) -> None:
        """Проверяет допуск игрока и доступ к определению текущего итога."""
        token = "fixture-token"
        storage = AsyncMock()
        storage.operate.return_value = {"revision": 1}
        storage.get_definition_permission.return_value = False
        catalog = Mock()
        catalog.lookup.return_value = {"definitions": ["Пояснение слова."]}
        app = create_app(
            storage, catalog, origin="https://quordle.test",
            token=token, allowed={123},
        )
        async with TestClient(TestServer(app)) as client:
            host = {"Host": "quordle.test"}
            response = await client.post("/api/state", json={}, headers=host)
            self.assertEqual(response.status, 401)
            foreign = host | {"X-Telegram-Init-Data": signed_data(321, token)}
            response = await client.post("/api/state", json={}, headers=foreign)
            self.assertEqual(response.status, 403)
            signed = host | {"X-Telegram-Init-Data": signed_data(123, token)}
            response = await client.post("/api/state", json={}, headers=signed)
            self.assertEqual(await response.json(), {"revision": 1})
            spoofed = signed | {"Origin": "https://elsewhere.test"}
            response = await client.post("/api/state", json={}, headers=spoofed)
            self.assertEqual(response.status, 403)
            response = await client.post(
                "/api/definition", json={"word": "ПАРУС"}, headers=signed
            )
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())["error"], "word_not_revealed")
            catalog.lookup.assert_not_called()
            storage.get_definition_permission.return_value = True
            response = await client.post(
                "/api/definition", json={"word": "парус"}, headers=signed
            )
            self.assertEqual(response.status, 200)
            card = await response.json()
            self.assertEqual(card["definition"], "Пояснение слова.")
            storage.get_definition_permission.assert_awaited_with(123, "ПАРУС")
            catalog.lookup.assert_called_once_with("ПАРУС")
            response = await client.get("/storage.py")
            self.assertEqual(response.status, 404)


@unittest.skipUnless(
    os.getenv("QUORDLE_RUN_DB_TESTS") == "1", "Требуется выделенный Docker PostgreSQL"
)
class DatabaseTest(unittest.IsolatedAsyncioTestCase):
    """Тестирует сохранение на отдельном синтетическом профиле."""

    async def asyncSetUp(self) -> None:
        """Подключает только базу локального стенда и выбирает синтетический ID."""
        config = dotenv_values(PG_FILE, interpolate=False)
        self.pool = await asyncpg.create_pool(
            host="127.0.0.1",
            port=55492,
            database=TEST_DATABASE,
            user="quordle_test",
            password=config["POSTGRES_PASSWORD"],
            init=initialise_connection,
            min_size=1,
            max_size=3,
            timeout=5,
        )
        self.storage = Storage(self.pool, QuordleEngine())
        await self.storage.initialise_test_database()
        self.player = Player(-(uuid4().int % (1 << 50) + 100), "Автотест")

    async def asyncTearDown(self) -> None:
        """Удаляет только запись тестового профиля и закрывает пул."""
        async with self.pool:
            await self.pool.execute(
                "delete from quordle_players where user_id = $1", self.player.user_id
            )

    async def test_state_duplicate_draft_partial_and_next(self) -> None:
        """Совмещает восстановление, дубль, результат 3/4 и новую партию."""
        player = self.player
        operate = self.storage.operate
        first = await operate(player, "state", {})
        self.assertTrue(all(board["answer"] is None for board in first["game"]["boards"]))
        self.assertNotIn("history", first)
        self.assertNotIn("deck", first)

        drafted = await operate(
            player, "draft", {"revision": first["revision"], "draft": "А С"}
        )
        restored = await operate(player, "state", {})
        self.assertEqual(restored["game"]["draft"], "А С")
        self.assertEqual(restored["revision"], drafted["revision"])
        with self.assertRaisesRegex(GameError, "словаре"):
            await operate(
                player, "guess", {"revision": restored["revision"], "word": "ЫЫЫЫЫ"}
            )
        game_data = await self.pool.fetchval(
            "select game_data from quordle_players where user_id = $1", player.user_id
        )
        answers = game_data["history"][-1]["answers"]
        payloads = [
            {"revision": restored["revision"], "word": answers[0], "request_id": str(uuid4())}
            for _ in range(2)
        ]
        concurrent = await asyncio.gather(
            *(operate(player, "guess", payload) for payload in payloads),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(x, RevisionConflict) for x in concurrent), 1)
        state = next(x for x in concurrent if isinstance(x, dict))
        accepted = payloads[concurrent.index(state)]
        replay = await operate(player, "guess", accepted)
        self.assertEqual(replay["game"]["attempts"], 1)
        self.assertEqual(replay["game"]["draft"], "")
        self.assertFalse(await self.storage.get_definition_permission(player.user_id, answers[0]))

        filler = next(word for word in ("ПАРУС", "КОШКА", "ГРУША", "ЛИМОН", "КНИГА")
                      if word not in answers)
        for word in answers[1:3] + [filler] * 6:
            state = await operate(
                player, "guess", {"revision": state["revision"], "word": word}
            )
        game = state["game"]
        self.assertEqual((game["status"], game["attempts"], game["solved"]), ("lost", 9, 3))
        self.assertEqual(game["result_score"], 30)
        self.assertEqual([len(x["rows"]) for x in game["boards"]], [1, 2, 3, 9])
        self.assertEqual(state["stats"]["played"], 1)
        self.assertEqual(state["stats"]["wins"], 0)
        self.assertEqual(state["stats"]["solved_words"], 3)
        can_define = self.storage.get_definition_permission
        for board in game["boards"]:
            self.assertTrue(await can_define(player.user_id, board["answer"]))
        self.assertFalse(await can_define(player.user_id, filler))
        new = await operate(player, "next", {"revision": state["revision"]})
        self.assertEqual(new["game"]["attempts"], 0)
        for answer in answers:
            self.assertFalse(await can_define(player.user_id, answer))
