"""Правила КВОРДЛИ: четыре поля, общие ходы и личная колода."""

from __future__ import annotations

import json
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from prototypes.wordli.engine import (
    DEFAULT_DICTIONARY,
    STATE_PRIORITY,
    GameError,
    evaluate_guess,
    normalize_draft,
    normalize_word,
)

BOARD_COUNT = 4
MAX_ATTEMPTS = 9
MIN_RATED_GAMES = 5
Profile = dict[str, Any]
Record = dict[str, Any]


def _today(value: date | None) -> date:
    """Возвращает игровую дату UTC."""
    day = datetime.now(timezone.utc).date() if value is None else value
    if type(day) is not date or day == date.max:
        raise GameError("invalid_day", "Некорректная игровая дата.")
    return day


def _found(record: Record) -> dict[str, int]:
    """Находит номер первого успешного хода для каждого ответа."""
    found: dict[str, int] = {}
    for move, guess in enumerate(record["guesses"], 1):
        if guess in record["answers"]:
            found.setdefault(guess, move)
    return found


def _status(record: Record, solved: int, day: date) -> str:
    """Определяет исход, не превращая просрочку в поражение."""
    if solved == BOARD_COUNT:
        return "won"
    if len(record["guesses"]) == MAX_ATTEMPTS:
        return "lost"
    return "expired" if record["day"] < day.isoformat() else "in_progress"


def _answer_revealed(status: str, solved: bool) -> bool:
    """Проверяет, раскрыт ли ответ этого поля в публичном состоянии."""
    return solved or status in {"won", "lost"}


def _points(status: str, solved: int, attempts: int) -> int | None:
    """Оценивает только завершённую партию."""
    if status == "won":
        return 60 + 8 * (MAX_ATTEMPTS - attempts)
    return 10 * solved if status == "lost" else None


def _rating(points: int, played: int) -> int | None:
    """Округляет до целого только окончательный рейтинг."""
    if played < MIN_RATED_GAMES:
        return None
    return (200 * points + played) // (2 * played)


class QuordleEngine:
    """Обрабатывает переданный профиль; сам не хранит игровые сессии."""

    def __init__(
        self,
        dictionary_path: Path | None = None,
        rng: random.Random | None = None,
    ) -> None:
        """Загружает общий с 5БУКВ фиксированный словарь."""
        path = dictionary_path or DEFAULT_DICTIONARY
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        self.dictionary_id: str = data["dictionary_id"]
        self._words = frozenset(data["words"])
        self._answers = tuple(data["answers"])
        if (
            len(set(self._answers)) != len(self._answers)
            or len(self._answers) < BOARD_COUNT
            or not self._words.issuperset(self._answers)
        ):
            raise ValueError("Некорректный набор ответов КВОРДЛИ")
        self._rng = rng if rng is not None else random.SystemRandom()

    def _deal(self, profile: Profile, day: date, index: int) -> None:
        """Резервирует четыре ответа; остаток двух циклов не смешивает."""
        if not profile:
            profile.update(history=[], deck=[], cycle=0, dictionary_id=None)
        if (
            profile["dictionary_id"] != self.dictionary_id
            or len(profile["deck"]) < BOARD_COUNT
        ):
            deck = list(self._answers)
            self._rng.shuffle(deck)
            profile.update(
                deck=deck, cycle=profile["cycle"] + 1,
                dictionary_id=self.dictionary_id,
            )
        deck = profile["deck"]
        answers = deck[-BOARD_COUNT:]
        del deck[-BOARD_COUNT:]
        profile["history"].append({
            "day": day.isoformat(),
            "index": index,
            "cycle": profile["cycle"],
            "dictionary_id": self.dictionary_id,
            "answers": answers,
            "guesses": [],
            "draft": "",
        })

    @staticmethod
    def _current(profile: Profile, day: date) -> Record:
        """Отклоняет устаревшее действие, не расходуя ход новой игры."""
        history = profile.get("history")
        if not history or history[-1]["day"] != day.isoformat():
            raise GameError("day_changed", "Откройте актуальную партию.", 409)
        return history[-1]

    def _active(self, profile: Profile, day: date) -> Record:
        """Возвращает текущую незавершённую партию для ввода."""
        record = self._current(profile, day)
        if _status(record, len(_found(record)), day) != "in_progress":
            raise GameError("game_finished", "Эта партия уже завершена.", 409)
        return record

    def bootstrap(self, profile: Profile, today: date | None = None) -> dict:
        """Восстанавливает игру, сохраняя частичный итог до явного перехода."""
        day = _today(today)
        history = profile.get("history", [])
        if history and history[-1]["day"] > day.isoformat():
            raise GameError(
                "invalid_day", "Дата партии опережает серверную.", 409
            )
        if not history or history[-1]["day"] != day.isoformat():
            if history:
                last = history[-1]
                solved = len(_found(last))
                if solved and _status(last, solved, day) == "expired":
                    return self.public_state(profile, day)
            self._deal(profile, day, 0)
        return self.public_state(profile, day)

    def submit(
        self, profile: Profile, word: str, today: date | None = None,
    ) -> dict:
        """Проверяет одну общую попытку сразу на всех открытых полях."""
        day = _today(today)
        record = self._active(profile, day)
        guess = normalize_word(word)
        if guess not in self._words and guess not in record["answers"]:
            raise GameError("unknown_word", "Такого слова нет в словаре.")
        record["guesses"].append(guess)
        record["draft"] = ""
        return self.public_state(profile, day)

    def set_draft(
        self, profile: Profile, draft: str, today: date | None = None,
    ) -> dict:
        """Сохраняет общий черновик с позициями введённых букв."""
        day = _today(today)
        record = self._active(profile, day)
        record["draft"] = normalize_draft(draft)
        return self.public_state(profile, day)

    def next_game(self, profile: Profile, today: date | None = None) -> dict:
        """Открывает новую четвёрку после результата или смены дня."""
        day = _today(today)
        history = profile.get("history", [])
        if history and history[-1]["day"] < day.isoformat():
            self._deal(profile, day, 0)
            return self.public_state(profile, day)
        record = self._current(profile, day)
        if _status(record, len(_found(record)), day) == "in_progress":
            raise GameError(
                "game_in_progress", "Сначала завершите партию.", 409
            )
        self._deal(profile, day, record["index"] + 1)
        return self.public_state(profile, day)

    @staticmethod
    def first_game_completed(profile: Profile, day: date) -> bool:
        """Сообщает, окончена ли первая партия указанных игровых суток."""
        target = _today(day).isoformat()
        for record in reversed(profile.get("history", [])):
            if record["day"] < target:
                break
            if record["day"] == target and record["index"] == 0:
                return _status(record, len(_found(record)), day) in {
                    "won", "lost",
                }
        return False

    def can_define(
        self, profile: Profile, word: str, today: date | None = None,
    ) -> bool:
        """Разрешает определения раскрытых ответов только на текущем итоге."""
        value = normalize_word(word)
        history = profile.get("history")
        if not history:
            return False
        record = history[-1]
        found = _found(record)
        status = _status(record, len(found), _today(today))
        return (
            status != "in_progress"
            and value in record["answers"]
            and _answer_revealed(status, value in found)
        )

    def stats(self, profile: Profile, today: date | None = None) -> dict:
        """Считает статистику всех циклов, не оценивая просроченные партии."""
        day = _today(today)
        played = wins = streak = maximum = points = winning_attempts = 0
        solved_words = expired_solved_words = 0
        previous_score = None
        distribution = [0] * (MAX_ATTEMPTS - BOARD_COUNT + 1)
        for record in profile.get("history", []):
            solved = len(_found(record))
            solved_words += solved
            attempts = len(record["guesses"])
            status = _status(record, solved, day)
            if status == "expired":
                expired_solved_words += solved
            result = _points(status, solved, attempts)
            if result is None:
                continue
            previous_score = _rating(points, played)
            played += 1
            points += result
            if status == "won":
                wins += 1
                streak += 1
                maximum = max(maximum, streak)
                winning_attempts += attempts
                distribution[attempts - BOARD_COUNT] += 1
            else:
                streak = 0
        score = _rating(points, played)
        delta = (
            score - previous_score
            if score is not None and previous_score is not None
            else None
        )
        return {
            "played": played, "wins": wins, "solved_words": solved_words,
            "expired_solved_words": expired_solved_words,
            "win_rate": wins * 100 / played if played else 0.0,
            "win_streak": streak, "max_streak": maximum,
            "average": winning_attempts / wins if wins else None,
            "distribution": distribution, "score": score,
            "score_delta": delta or None,
        }

    def public_state(self, profile: Profile, today: date | None = None) -> dict:
        """Готовит представление без колоды и скрытых ответов."""
        day = _today(today)
        if not profile.get("history"):
            raise GameError("no_game", "Сначала откройте игру.", 409)
        record = profile["history"][-1]
        guesses = record["guesses"]
        found = _found(record)
        status = _status(record, len(found), day)
        boards = []
        for answer in record["answers"]:
            solved_at = found.get(answer)
            rows = []
            keyboard: dict[str, str] = {}
            for guess in guesses[:solved_at]:
                states = evaluate_guess(answer, guess)
                rows.append({"word": guess, "states": states})
                for letter, state in zip(guess, states):
                    if STATE_PRIORITY[state] > STATE_PRIORITY.get(
                        keyboard.get(letter, ""), 0
                    ):
                        keyboard[letter] = state
            revealed = _answer_revealed(status, solved_at is not None)
            boards.append({
                "solved": solved_at is not None, "solved_at": solved_at,
                "rows": rows, "keyboard": keyboard,
                "answer": answer if revealed else None,
            })
        expires = datetime.combine(day + timedelta(days=1), time(), timezone.utc)
        return {
            "day": day.isoformat(),
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "dictionary": {
                "id": self.dictionary_id, "size": len(self._words),
                "answers": len(self._answers),
            },
            "game": {
                "id": f"{record['day']}:{record['index']}",
                "day": record["day"],
                "cycle": record["cycle"],
                "dictionary_id": record["dictionary_id"],
                "status": status, "draft": record["draft"],
                "attempts": len(guesses), "solved": len(found),
                "result_score": _points(status, len(found), len(guesses)),
                "boards": boards,
            },
            "stats": self.stats(profile, day),
        }
