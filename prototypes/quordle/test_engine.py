"""Автономные проверки правил КВОРДЛИ без БД и Telegram."""

import json
import random
import unittest
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import patch

from prototypes.quordle.engine import GameError, QuordleEngine
from prototypes.wordli.engine import LocalGameEngine


class QuordleEngineTest(unittest.TestCase):
    """Проверяет общий ввод, оценку партий и независимость личных циклов."""

    def setUp(self) -> None:
        """Готовит словарь с неполным остатком в два ответа."""
        self.answers = [
            "МАСКА", "КАССА", "СОСНА", "НАСОС", "КОШКА",
            "ЛОЖКА", "БУЛКА", "КОФТА", "МЕТРО", "НОРКА",
        ]
        payload = {
            "dictionary_id": "test-quordle",
            "words": self.answers + ["АРГОН", "АКТЕР"],
            "answers": self.answers,
        }
        with patch("pathlib.Path.read_text", return_value=json.dumps(payload)):
            self.engine = QuordleEngine(rng=random.Random(2))
        self.day = date(2026, 11, 21)
        self.profile = {}
        self.engine.bootstrap(self.profile, self.day)

    @property
    def answers_now(self) -> list[str]:
        """Возвращает четыре зафиксированных ответа тестовой партии."""
        return self.profile["history"][-1]["answers"]

    def move(self, word: str) -> dict:
        """Отправляет допустимое слово от текущего игрока."""
        return self.engine.submit(self.profile, word, self.day)

    def solve(self, attempts: int = 4) -> dict:
        """Завершает партию за указанное число ходов."""
        for _ in range(attempts - 4):
            self.move("АРГОН")
        for answer in self.answers_now:
            state = self.move(answer)
        return state

    def test_bootstrap_persists_private_draw_without_revealing_it(self) -> None:
        """Повторное открытие не меняет профиль и не выдаёт ответы."""
        before = deepcopy(self.profile)
        view = self.engine.bootstrap(self.profile, self.day)
        self.assertEqual(before, self.profile)
        self.assertEqual(view["expires_at"], "2026-11-22T00:00:00Z")
        self.assertEqual(len(set(self.answers_now)), 4)
        self.assertEqual(view["game"]["status"], "in_progress")
        self.assertEqual(view["game"]["attempts"], 0)
        self.assertNotIn("history", view)
        self.assertNotIn("deck", view)
        self.assertTrue(all(b["answer"] is None for b in view["game"]["boards"]))

    def test_unknown_draft_and_repeat(self) -> None:
        """Ошибки сохраняют черновик, а повтор принятого слова тратит ход."""
        state = self.engine.set_draft(self.profile, " а ё ", self.day)
        self.assertEqual(state["game"]["draft"], " А Е")
        before = deepcopy(self.profile)
        for bad in ["ААААА", "А", "ABCDE"]:
            with self.assertRaises(GameError):
                self.move(bad)
        with self.assertRaises(GameError):
            self.engine.set_draft(self.profile, "123", self.day)
        with self.assertRaises(GameError):
            self.engine.next_game(self.profile, self.day)
        self.assertEqual(self.profile, before)
        self.move("актёр")
        state = self.move("АКТЕР")
        self.assertEqual(state["game"]["attempts"], 2)
        self.assertEqual(state["game"]["draft"], "")
        self.assertEqual(state["game"]["boards"][0]["rows"][0]["word"], "АКТЕР")

    def test_per_board_presence_and_frozen_keyboard(self) -> None:
        """Каждое поле оценивает повторы независимо и замирает при решении."""
        self.profile["history"][-1]["answers"] = self.answers[:4]
        first = self.move("КАССА")
        board = first["game"]["boards"][0]
        self.assertEqual(
            board["rows"][0]["states"],
            ["present", "correct", "correct", "present", "correct"],
        )
        self.assertEqual(board["keyboard"]["С"], "correct")
        frozen = first["game"]["boards"][1]
        self.assertEqual(frozen["solved_at"], 1)
        self.assertEqual(frozen["answer"], "КАССА")
        next_view = self.move("АРГОН")
        self.assertEqual(frozen, next_view["game"]["boards"][1])
        self.assertEqual(len(next_view["game"]["boards"][0]["rows"]), 2)

    def test_wordli_feedback_on_four_boards_after_reload(self) -> None:
        """Все поля используют правила 5БУКВ при ходе и после перезагрузки."""
        answers = ["КРОНА", "КОФТА", "МЕТРО", "БУЛКА"]
        guesses = ["ПАРОМ", "СОПЛО"]
        quordle = QuordleEngine()
        profile: dict = {}
        quordle.bootstrap(profile, self.day)
        profile["history"][0]["answers"] = answers
        for word in guesses:
            state = quordle.submit(profile, word, self.day)
        boards = state["game"]["boards"]
        duplicate_marks = [
            (b["rows"][1]["states"][1], b["rows"][1]["states"][4]) for b in boards
        ]
        self.assertEqual(duplicate_marks, [
            ("present", "present"), ("correct", "present"),
            ("present", "correct"), ("absent", "absent"),
        ])
        self.assertEqual(
            [board["keyboard"]["О"] for board in boards],
            ["present", "correct", "correct", "absent"],
        )
        wordli = LocalGameEngine()
        for answer, board in zip(answers, boards):
            record = {
                "day": self.day.isoformat(), "index": 0,
                "answer": answer, "dictionary_id": wordli.dictionary_id,
                "guesses": guesses, "draft": "",
            }
            single = wordli.bootstrap([record], self.day)["game"]
            self.assertEqual(board["rows"], single["rows"])
            self.assertEqual(board["keyboard"], single["keyboard"])
        restored = quordle.bootstrap(json.loads(json.dumps(profile)), self.day)
        self.assertEqual(restored, state)

    def test_partial_loss_and_definition_rights(self) -> None:
        """После поражения все раскрытые ответы кликабельны до новой игры."""
        answers = list(self.answers_now)
        for answer in answers[:3]:
            self.move(answer)
        for _ in range(6):
            view = self.move("АРГОН")
        game = view["game"]
        self.assertEqual((game["status"], game["result_score"]), ("lost", 30))
        self.assertEqual([b["answer"] for b in game["boards"]], answers)
        for answer in answers:
            self.assertTrue(self.engine.can_define(self.profile, answer, self.day))
        self.assertFalse(self.engine.can_define(self.profile, "АРГОН", self.day))
        stats = view["stats"]
        self.assertEqual((stats["played"], stats["solved_words"]), (1, 3))
        self.assertEqual((stats["wins"], stats["average"]), (0, None))
        with self.assertRaises(GameError):
            self.move(answers[3])
        self.engine.next_game(self.profile, self.day)
        for answer in answers:
            self.assertFalse(self.engine.can_define(self.profile, answer, self.day))

    def test_definitions_open_on_win_not_on_active_board(self) -> None:
        """Даже закрытое поле не открывает справочник посреди игры."""
        answers = list(self.answers_now)
        game = self.move(answers[0])["game"]
        self.assertEqual(game["boards"][0]["answer"], answers[0])
        for answer in answers:
            self.assertFalse(self.engine.can_define(self.profile, answer, self.day))
        for answer in answers[1:]:
            game = self.move(answer)["game"]
        self.assertEqual(game["status"], "won")
        for board in game["boards"]:
            self.assertTrue(
                self.engine.can_define(self.profile, board["answer"], self.day)
            )
        self.assertFalse(self.engine.can_define(self.profile, "АРГОН", self.day))

    def test_real_dictionary_and_independent_players(self) -> None:
        """Второй профиль получает собственные ответы, ходы и историю."""
        engine = QuordleEngine(rng=random.Random(9))
        first: dict = {}
        second: dict = {}
        start = engine.bootstrap(first, self.day)
        other = engine.bootstrap(second, self.day)
        self.assertGreaterEqual(start["dictionary"]["answers"], 4)
        self.assertEqual(start["dictionary"], other["dictionary"])
        self.assertNotEqual(
            first["history"][0]["answers"], second["history"][0]["answers"]
        )
        answers = first["history"][0]["answers"]
        for answer in answers[:2]:
            engine.submit(first, answer, self.day)
        for _ in range(7):
            view = engine.submit(first, answers[0], self.day)
        self.assertEqual(view["game"]["result_score"], 20)
        self.assertEqual(engine.public_state(second, self.day), other)

    def test_rating_distribution_and_delta(self) -> None:
        """Проверяет шкалу побед, накопление очков и порог пяти партий."""
        for number, attempts in enumerate(range(4, 10), 1):
            if number > 1:
                self.engine.next_game(self.profile, self.day)
            view = self.solve(attempts)
            self.assertEqual(view["game"]["result_score"], 60 + 8 * (9 - attempts))
            if number < 5:
                self.assertIsNone(view["stats"]["score"])
            if number == 5:
                self.assertEqual(view["stats"]["score"], 8400)
                self.assertIsNone(view["stats"]["score_delta"])
        stats = view["stats"]
        self.assertEqual(stats["score"], 8000)
        self.assertEqual(stats["score_delta"], -400)
        self.assertEqual(stats["distribution"], [1] * 6)
        self.assertEqual((stats["win_streak"], stats["max_streak"]), (6, 6))
        self.assertEqual(stats["average"], 6.5)
        self.engine.next_game(self.profile, self.day)
        self.move(self.answers_now[0])
        for _ in range(8):
            view = self.move("АРГОН")
        self.assertEqual(view["stats"]["score"], 7000)
        self.assertEqual(view["stats"]["win_streak"], 0)
        self.engine.next_game(self.profile, self.day)
        for _ in range(9):
            view = self.move("АРГОН")
        self.assertEqual(view["game"]["result_score"], 0)
        self.assertEqual(view["stats"]["score"], 6125)
        self.assertEqual(view["game"]["solved"], 0)
        for board in view["game"]["boards"]:
            self.assertTrue(
                self.engine.can_define(self.profile, board["answer"], self.day)
            )

    def test_cycle_and_utc_expiry_keep_word_progress(self) -> None:
        """Просрочка не проигрыш; новый полный цикл не обнуляет историю."""
        first = set(self.answers_now)
        solved = self.answers_now[0]
        self.move(solved)
        tomorrow = self.day + timedelta(days=1)
        before = deepcopy(self.profile)
        with self.assertRaises(GameError):
            self.engine.submit(self.profile, "АРГОН", tomorrow)
        self.assertEqual(self.profile, before)
        expired = self.engine.public_state(self.profile, tomorrow)
        self.assertEqual(expired["game"]["status"], "expired")
        self.assertIsNone(expired["game"]["result_score"])
        self.assertTrue(self.engine.can_define(self.profile, solved, tomorrow))
        for answer in self.answers_now[1:]:
            self.assertFalse(self.engine.can_define(self.profile, answer, tomorrow))
        self.day = tomorrow
        self.assertEqual(self.engine.bootstrap(self.profile, self.day), expired)
        self.assertEqual(
            expired["game"]["day"], (tomorrow - timedelta(days=1)).isoformat()
        )
        self.assertEqual(expired["day"], tomorrow.isoformat())
        self.assertEqual(expired["expires_at"], "2026-11-23T00:00:00Z")
        self.assertEqual(self.profile, before)
        self.assertIsNone(expired["game"]["boards"][1]["answer"])
        active = self.engine.next_game(self.profile, self.day)
        self.assertEqual(active["game"]["id"], f"{tomorrow}:0")
        self.assertIsNone(active["game"]["boards"][0]["answer"])
        self.assertFalse(self.engine.can_define(self.profile, solved, self.day))
        self.assertTrue(first.isdisjoint(self.answers_now))
        self.assertEqual(active["game"]["cycle"], 1)
        self.assertEqual(active["stats"]["played"], 0)
        self.assertEqual(active["stats"]["expired_solved_words"], 1)
        self.assertEqual(len(self.profile["deck"]), 2)
        self.solve()
        state = self.engine.next_game(self.profile, self.day)
        self.assertEqual(state["game"]["cycle"], 2)
        self.assertEqual(len(self.profile["deck"]), 6)
        self.assertEqual(len(self.profile["history"]), 3)
        self.assertEqual(state["stats"]["solved_words"], 5)

    def test_daily_reminder_and_automatic_new_day(self) -> None:
        """Напоминание зависит от результата первой партии своих суток."""
        completed = self.engine.first_game_completed
        self.assertFalse(completed({}, self.day))
        self.move("АРГОН")
        self.assertFalse(completed(self.profile, self.day))
        first_day = self.day
        self.day += timedelta(days=1)
        fresh = self.engine.bootstrap(self.profile, self.day)
        self.assertEqual(fresh["game"]["id"], f"{self.day}:0")
        self.assertEqual(fresh["game"]["status"], "in_progress")
        self.assertFalse(completed(self.profile, first_day))
        for _ in range(9):
            self.move("АРГОН")
        self.assertTrue(completed(self.profile, self.day))
        self.engine.next_game(self.profile, self.day)
        self.assertTrue(completed(self.profile, self.day))
        self.solve()
        self.day += timedelta(days=1)
        self.assertFalse(completed(self.profile, self.day))
        state = self.engine.bootstrap(self.profile, self.day)
        self.assertEqual(state["game"]["id"], f"{self.day}:0")
        self.solve()
        self.assertTrue(completed(self.profile, self.day))


if __name__ == "__main__":
    unittest.main()
