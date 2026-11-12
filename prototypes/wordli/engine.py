"""Чистые правила локальной игры без Telegram, базы данных и сети."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from math import floor, gcd, sqrt
from pathlib import Path
from typing import Any

WORD_LENGTH = 5
MAX_ATTEMPTS = 6
MIN_RATED_GAMES = 5
MAX_HISTORY_RECORDS = 5000
# Совместимость с проверками и сохранениями первого локального этапа.
MAX_HISTORY_DAYS = MAX_HISTORY_RECORDS
MAX_GAMES_PER_DAY = 1000
DEFAULT_DICTIONARY = Path(__file__).with_name("data") / "ru5.json"
WORD_PATTERN = re.compile(r"[А-Я]{5}\Z")
DRAFT_PATTERN = re.compile(r"[А-Я ]{0,5}\Z")
DAY_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
STATE_PRIORITY = {"absent": 1, "present": 2, "correct": 3}
LEGACY_DICTIONARY_ID = "ru5-opencorpora-wordfreq-v1-165050f1e2b8"
SCORE_VERSION = "progress-quality-v2"


class GameError(ValueError):
    """Ошибка игрового ввода с безопасным сообщением и кодом ответа."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        """Сохраняет тип ошибки без служебных данных и ответа игры."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def normalize_word(value: str) -> str:
    """Приводит пять русских букв к верхнему регистру, считая Ё равной Е."""
    if not isinstance(value, str):
        raise GameError("invalid_word", "Введите слово из пяти русских букв.")
    word = unicodedata.normalize("NFC", value).strip().upper().replace("Ё", "Е")
    if not WORD_PATTERN.fullmatch(word):
        raise GameError("invalid_word", "Введите слово из пяти русских букв.")
    return word


def normalize_draft(value: str) -> str:
    """Сохраняет позиции букв и пустых ячеек незавершённой строки."""
    if not isinstance(value, str) or len(value) > 64:
        raise GameError("invalid_draft", "Некорректная строка ввода.")
    draft = unicodedata.normalize("NFC", value).upper().replace("Ё", "Е")
    draft = draft.rstrip(" ")
    if not DRAFT_PATTERN.fullmatch(draft):
        raise GameError("invalid_draft", "Некорректная строка ввода.")
    return draft


def evaluate_guess(answer: str, guess: str) -> list[str]:
    """Сначала отмечает точные позиции, затем оставшиеся совпадения букв."""
    answer = normalize_word(answer)
    guess = normalize_word(guess)
    states = ["absent"] * WORD_LENGTH
    remaining: Counter[str] = Counter()
    for index, (expected, entered) in enumerate(zip(answer, guess)):
        if expected == entered:
            states[index] = "correct"
        else:
            remaining[expected] += 1
    for index, entered in enumerate(guess):
        if states[index] == "correct":
            continue
        if remaining[entered]:
            states[index] = "present"
            remaining[entered] -= 1
    return states


def calculate_score(
    played: int,
    wins: int,
    average: float | None,
    max_streak: int,
    pool_size: int,
) -> int | None:
    """Считает целые очки прогресса и качества после пяти партий."""
    if played < MIN_RATED_GAMES:
        return None
    if not wins or average is None:
        return 0
    progress = sqrt(min(played, pool_size) / pool_size)
    quality = (7 - average) / 6
    series = sqrt(min(max_streak, pool_size) / pool_size)
    value = floor(1000 * progress * (wins / played) * (4 + 4 * quality + 2 * series))
    return max(0, min(value, 10000))


def _invalid_history() -> GameError:
    """Формирует одинаковую ошибку для повреждённого локального сохранения."""
    return GameError(
        "invalid_history",
        "Локальная история повреждена. Сохранение не изменено.",
    )


def _read_day(value: Any) -> date:
    """Читает только однозначную календарную дату в формате ISO."""
    if not isinstance(value, str) or not DAY_PATTERN.fullmatch(value):
        raise _invalid_history()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _invalid_history() from None


def _today(value: date | None) -> date:
    """Определяет день по UTC; явная дата нужна только чистым тестам."""
    current = datetime.now(timezone.utc).date() if value is None else value
    if type(current) is not date or current == date.max:
        raise GameError("invalid_day", "Некорректный игровой день.")
    return current


def _read_index(value: Any) -> int:
    """Проверяет ограниченный номер партии внутри игрового дня."""
    if type(value) is not int or not 0 <= value < MAX_GAMES_PER_DAY:
        raise GameError("invalid_index", "Некорректный номер партии.")
    return value


class LocalGameEngine:
    """Проверяет партии и пересчитывает присланную локальную историю."""

    def __init__(self, dictionary_path: Path | None = None) -> None:
        """Загружает фиксированный словарь, не читая настройки бота."""
        path = DEFAULT_DICTIONARY if dictionary_path is None else Path(dictionary_path)
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if (
            not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
        ):
            raise ValueError("Некорректный формат игрового словаря")
        if payload["schema_version"] != 1 or payload.get("normalization") != "Ё=Е":
            raise ValueError("Неподдерживаемая версия игрового словаря")
        identifier = payload.get("dictionary_id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128:
            raise ValueError("Не задан идентификатор словаря")
        words = self._read_dictionary_words(payload.get("words"))
        answers = self._read_dictionary_words(payload.get("answers"))
        if not answers.issubset(words):
            raise ValueError("Ответы должны входить в словарь допустимых слов")
        self.dictionary_id = identifier
        self.dictionary_size = len(words)
        self._dictionary_path = path
        self._archive_root = path.parent / "archive"
        self._archive_engines: dict[str, LocalGameEngine] = {}
        self._words = frozenset(words)
        self._answer_words = frozenset(answers)
        # Фиксированный порядок не зависит от hash(), процесса и порядка JSON.
        self._answers = tuple(
            sorted(
                answers,
                key=lambda word: hashlib.sha256(
                    ("wordli-local-v01\0" + word).encode("utf-8")
                ).digest(),
            )
        )

    def dictionary_for_id(self, identifier: str) -> LocalGameEngine:
        """Открывает только известный архив словаря без произвольных путей."""
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,128}", identifier
        ):
            raise GameError("dictionary_changed", "Неизвестная версия словаря.", 409)
        if identifier == self.dictionary_id:
            return self
        if identifier in self._archive_engines:
            return self._archive_engines[identifier]
        path = self._archive_root / f"{identifier}.json"
        if not path.is_file():
            raise GameError("dictionary_changed", "Неизвестная версия словаря.", 409)
        archived = LocalGameEngine(path)
        archived._archive_root = self._archive_root
        if archived.dictionary_id != identifier:
            raise GameError(
                "dictionary_changed", "Версия архива словаря не совпадает.", 409
            )
        self._archive_engines[identifier] = archived
        return archived

    def migrate_history(
        self, history: list, dictionary_id: str, today: date | None = None
    ) -> list[dict[str, Any]]:
        """Закрепляет прежние ответы, не пересчитывая их новым словарём."""
        archived = self.dictionary_for_id(dictionary_id)
        records = archived._read_history(history, _today(today))
        return [records[key] for key in sorted(records)]

    def _legacy_dictionary(self) -> LocalGameEngine:
        """Выбирает исходный словарь для старой браузерной истории."""
        if self._dictionary_path.resolve() == DEFAULT_DICTIONARY.resolve():
            archive = self._archive_root / f"{LEGACY_DICTIONARY_ID}.json"
            if archive.is_file():
                return self.dictionary_for_id(LEGACY_DICTIONARY_ID)
        return self

    def _new_record(
        self,
        records: dict[tuple[str, int], dict[str, Any]],
        day: date,
        index: int,
        player_id: int,
    ) -> dict[str, Any]:
        """Резервирует персональное слово, которое ещё не выдавалось."""
        used = {record["answer"] for record in records.values()}
        available = [word for word in self._answers if word not in used]
        if not available:
            raise GameError("no_more_words", "Все доступные слова уже сыграны.", 409)
        if player_id:
            answer = secrets.choice(available)
        else:
            preferred = self.word_for_game(day, index)
            answer = preferred if preferred in available else available[0]
        return {
            "day": day.isoformat(),
            "index": index,
            "guesses": [],
            "draft": "",
            "answer": answer,
            "dictionary_id": self.dictionary_id,
        }

    @staticmethod
    def _read_dictionary_words(value: Any) -> set[str]:
        """Проверяет непустой набор уже нормализованных уникальных слов."""
        if not isinstance(value, list) or not value or len(value) > 50000:
            raise ValueError("Некорректный список слов")
        if any(
            not isinstance(word, str) or not WORD_PATTERN.fullmatch(word)
            for word in value
        ):
            raise ValueError("Словарь должен содержать пять русских букв без Ё")
        words = set(value)
        if len(words) != len(value):
            raise ValueError("В словаре обнаружены повторяющиеся слова")
        return words

    def word_for_day(self, day: date) -> str:
        """Возвращает внутренний ответ дня без повторов в пределах цикла."""
        if type(day) is not date:
            raise ValueError("Ожидается календарная дата")
        return self._answers[day.toordinal() % len(self._answers)]

    def word_for_game(self, day: date, index: int = 0) -> str:
        """Выбирает слово партии, исключая дневной ответ из повторных игр."""
        _read_index(index)
        daily = self.word_for_day(day)
        if index == 0:
            return daily
        if len(self._answers) < 2:
            raise GameError(
                "no_more_words", "В словаре нет другого слова для игры.", 409
            )
        size = len(self._answers) - 1
        digest = hashlib.sha256(
            ("wordli-replay-v01\0" + day.isoformat()).encode("utf-8")
        ).digest()
        shift = int.from_bytes(digest[:8], "big") % size
        step = int.from_bytes(digest[8:16], "big") % size or 1
        # Взаимно простой шаг перебирает все остальные слова без повторов.
        while gcd(step, size) != 1:
            step = (step + 1) % size or 1
        position = ((index - 1) * step + shift) % size
        daily_position = day.toordinal() % len(self._answers)
        if position >= daily_position:
            position += 1
        return self._answers[position]

    def bootstrap(
        self,
        history: list,
        today: date | None = None,
        player_id: int = 0,
    ) -> dict[str, Any]:
        """Восстанавливает поле и статистику без изменения входа."""
        current = _today(today)
        records = self._read_history(history, current)
        day_key = current.isoformat()
        if not any(key[0] == day_key for key in records):
            used = {record["answer"] for record in records.values()}
            if len(records) >= MAX_HISTORY_RECORDS and self._answer_words - used:
                raise _invalid_history()
            if self._answer_words - used:
                records[(day_key, 0)] = self._new_record(records, current, 0, player_id)
        games = [
            self._evaluate_game(record, current)
            for _, record in sorted(records.items())
        ]
        game = next(
            (item for item in reversed(games) if item["day"] == day_key),
            {
                "day": day_key,
                "index": 0,
                "id": f"{day_key}:0",
                "guesses": [],
                "rows": [],
                "draft": "",
                "status": "exhausted",
                "outcome": None,
                "answer": None,
                "keyboard": {},
            },
        )
        used = {record["answer"] for record in records.values()}
        remaining = len(self._answer_words - used)
        expires = datetime.combine(current + timedelta(days=1), time(), timezone.utc)
        return {
            "day": current.isoformat(),
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "dictionary": {
                "id": self.dictionary_id,
                "size": self.dictionary_size,
                "answers": len(self._answers),
            },
            "history": [records[key] for key in sorted(records)],
            "game": game,
            "stats": self._statistics(games, current),
            "progression": self._progression(games),
            "availability": {"remaining": remaining, "exhausted": remaining == 0},
        }

    def statistics(self, history: list, today: date | None = None) -> dict[str, Any]:
        """Считает статистику без выдачи нового слова и изменения истории."""
        current = _today(today)
        records = self._read_history(history, current)
        games = [
            self._evaluate_game(record, current)
            for _, record in sorted(records.items())
        ]
        return self._statistics(games, current)

    def submit(
        self,
        history: list,
        day: str,
        guess: str,
        today: date | None = None,
        index: int = 0,
        player_id: int = 0,
    ) -> dict[str, Any]:
        """Принимает допустимый ход только в текущую незавершённую партию."""
        current = _today(today)
        if day != current.isoformat():
            raise GameError(
                "day_changed",
                "Игровой день сменился. Открыто новое слово.",
                409,
            )
        state = self.bootstrap(history, current, player_id=player_id)
        self._check_active_index(state, index)
        if state["game"]["status"] == "exhausted":
            raise GameError("no_more_words", "Все доступные слова уже сыграны.", 409)
        if state["game"]["outcome"] is not None:
            raise GameError("game_finished", "Эта партия уже завершена.", 409)
        word = normalize_word(guess)
        active = next(
            record
            for record in state["history"]
            if record["day"] == day and record["index"] == index
        )
        source = self.dictionary_for_id(active["dictionary_id"])
        if word not in self._words and word not in source._words:
            raise GameError("unknown_word", "Такого слова нет в словаре.")
        for record in state["history"]:
            if record["day"] == day and record["index"] == index:
                record["guesses"].append(word)
                record["draft"] = ""
                break
        return self.bootstrap(state["history"], current, player_id=player_id)

    def next_game(
        self,
        history: list,
        day: str,
        index: int,
        today: date | None = None,
        player_id: int = 0,
    ) -> dict[str, Any]:
        """Открывает следующую партию после окончания активной."""
        current = _today(today)
        if day != current.isoformat():
            raise GameError(
                "day_changed",
                "Игровой день сменился. Открыто новое слово.",
                409,
            )
        state = self.bootstrap(history, current, player_id=player_id)
        self._check_active_index(state, index)
        if state["game"]["status"] == "exhausted":
            raise GameError("no_more_words", "Все доступные слова уже сыграны.", 409)
        if state["game"]["outcome"] is None:
            raise GameError(
                "game_in_progress",
                "Сначала завершите текущую партию.",
                409,
            )
        if index + 1 >= MAX_GAMES_PER_DAY:
            raise GameError("daily_limit", "Достигнут лимит партий на сегодня.", 409)
        if state["availability"]["exhausted"]:
            raise GameError("no_more_words", "Все доступные слова уже сыграны.", 409)
        if len(state["history"]) >= MAX_HISTORY_RECORDS:
            raise GameError("history_limit", "Достигнут лимит локальной истории.", 409)
        records = {(item["day"], item["index"]): item for item in state["history"]}
        state["history"].append(
            self._new_record(records, current, index + 1, player_id)
        )
        return self.bootstrap(state["history"], current, player_id=player_id)

    @staticmethod
    def _check_active_index(state: dict[str, Any], index: int) -> None:
        """Отклоняет запрос вкладки, оставшейся на прошлой партии."""
        _read_index(index)
        if state["game"]["index"] != index:
            raise GameError(
                "game_changed",
                "Партия изменилась. Загружено актуальное поле.",
                409,
            )

    def _read_history(
        self, history: Any, today: date
    ) -> dict[tuple[str, int], dict[str, Any]]:
        """Проверяет и копирует историю, не доверяя сохранённым исходам."""
        if not isinstance(history, list) or len(history) > MAX_HISTORY_RECORDS:
            raise _invalid_history()
        records: dict[tuple[str, int], dict[str, Any]] = {}
        for raw in history:
            if not isinstance(raw, dict):
                raise _invalid_history()
            day = _read_day(raw.get("day"))
            try:
                index = _read_index(raw.get("index", 0))
                identifier = raw.get("dictionary_id")
                source = (
                    self.dictionary_for_id(identifier)
                    if identifier is not None
                    else self._legacy_dictionary()
                )
                answer = raw.get("answer")
                if answer is None:
                    answer = source.word_for_game(day, index)
                elif not isinstance(answer, str) or answer not in source._answer_words:
                    raise _invalid_history()
            except GameError:
                raise _invalid_history() from None
            key = (day.isoformat(), index)
            if day > today or key in records:
                raise _invalid_history()
            guesses = raw.get("guesses", [])
            if not isinstance(guesses, list) or len(guesses) > MAX_ATTEMPTS:
                raise _invalid_history()
            try:
                normalized = [normalize_word(word) for word in guesses]
            except GameError:
                raise _invalid_history() from None
            if any(
                word not in source._words and word not in self._words
                for word in normalized
            ):
                raise _invalid_history()
            if answer in normalized[:-1]:
                raise _invalid_history()
            draft = raw.get("draft", "")
            if not isinstance(draft, str):
                raise _invalid_history()
            try:
                draft = normalize_draft(draft)
            except GameError:
                raise _invalid_history() from None
            if len(normalized) == MAX_ATTEMPTS or answer in normalized:
                draft = ""
            records[key] = {
                "day": day.isoformat(),
                "index": index,
                "guesses": normalized,
                "draft": draft,
                "answer": answer,
                "dictionary_id": source.dictionary_id,
            }
        previous_day = None
        previous_index = -1
        previous_finished = True
        for (day_key, index), record in sorted(records.items()):
            if day_key != previous_day:
                previous_index = -1
                previous_finished = True
            if index != previous_index + 1 or not previous_finished:
                raise _invalid_history()
            answer = record["answer"]
            previous_finished = (
                len(record["guesses"]) == MAX_ATTEMPTS or answer in record["guesses"]
            )
            previous_day = day_key
            previous_index = index
        return records

    def _evaluate_game(self, record: dict[str, Any], today: date) -> dict[str, Any]:
        """Раскрашивает ходы, определяет исход и подсказки клавиатуры."""
        day = _read_day(record["day"])
        index = record["index"]
        answer = record["answer"]
        guesses = record["guesses"]
        rows = [
            {"word": word, "states": evaluate_guess(answer, word)} for word in guesses
        ]
        outcome = "win" if guesses and guesses[-1] == answer else None
        if outcome is None and len(guesses) == MAX_ATTEMPTS:
            outcome = "loss"
        status = {"win": "won", "loss": "lost"}.get(outcome, "in_progress")
        if outcome is None and day < today:
            status = "expired"
        keyboard: dict[str, str] = {}
        for row in rows:
            for letter, state in zip(row["word"], row["states"]):
                if STATE_PRIORITY[state] > STATE_PRIORITY.get(
                    keyboard.get(letter, ""), 0
                ):
                    keyboard[letter] = state
        return {
            "day": day.isoformat(),
            "index": index,
            "id": f"{day.isoformat()}:{index}",
            "guesses": guesses.copy(),
            "rows": rows,
            "draft": record["draft"],
            "status": status,
            "outcome": outcome,
            "answer": answer if outcome is not None else None,
            "keyboard": keyboard,
        }

    def _statistics(self, games: list[dict[str, Any]], today: date) -> dict[str, Any]:
        """Считает статистику и изменение очков за последнюю завершённую игру."""
        completed = [game for game in games if game["outcome"] is not None]
        played = len(completed)
        distribution = [0] * MAX_ATTEMPTS
        wins = 0
        attempts = 0
        streak = 0
        maximum = 0
        strict = 0
        previous_day = None
        previous_score = None
        for index, game in enumerate(completed):
            if index == played - 1:
                previous_score = calculate_score(
                    index,
                    wins,
                    attempts / wins if wins else None,
                    maximum,
                    len(self._answers),
                )
            day = _read_day(game["day"])
            if previous_day is not None and (day - previous_day).days > 1:
                strict = 0
            if game["outcome"] == "win":
                distribution[len(game["guesses"]) - 1] += 1
                wins += 1
                attempts += len(game["guesses"])
                streak += 1
                maximum = max(maximum, streak)
                strict += 1
            else:
                streak = 0
                strict = 0
            previous_day = day
        if previous_day is not None and (today - previous_day).days > 1:
            strict = 0
        average = attempts / wins if wins else None
        score = calculate_score(played, wins, average, maximum, len(self._answers))
        score_delta = (
            score - previous_score
            if score is not None and previous_score is not None
            else None
        )
        if score_delta == 0:
            score_delta = None
        return {
            "played": played,
            "wins": wins,
            "strict_streak": strict,
            "win_streak": streak,
            "max_streak": maximum,
            "average": average,
            "win_rate": wins / played * 100 if played else 0.0,
            "distribution": distribution,
            "score": score,
            "score_delta": score_delta,
            "score_version": SCORE_VERSION,
        }

    @staticmethod
    def _progression(games: list[dict[str, Any]]) -> dict[str, Any]:
        """Показывает окно пяти побед без выдачи будущих ответов."""
        wins = [game for game in games if game["outcome"] == "win"]
        total = len(wins)
        start = ((total - 1) // 5) * 5 + 1 if total else 1
        end = start + 4
        return {
            "total_wins": total,
            "start": start,
            "end": end,
            "nodes": [
                {
                    "number": number,
                    "word": wins[number - 1]["answer"] if number <= total else None,
                    "complete": number <= total,
                }
                for number in range(start, end + 1)
            ],
        }
