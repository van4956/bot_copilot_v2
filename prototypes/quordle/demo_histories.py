"""Генерирует в памяти воспроизводимые игровые истории для тестового стенда."""

import random
from copy import copy
from datetime import date, timedelta
from typing import Any

from prototypes.quordle.engine import BOARD_COUNT, QuordleEngine
from prototypes.quordle.engine import MAX_ATTEMPTS as QUORDLE_ATTEMPTS
from prototypes.wordli.engine import LocalGameEngine
from prototypes.wordli.engine import MAX_ATTEMPTS as WORDLI_ATTEMPTS


def _days(rng: random.Random, today: date) -> list[date]:
    """Выбирает 12–30 разных прошедших игровых дней с пропусками."""
    total = rng.randint(12, 30)
    offsets = rng.sample(range(1, total + rng.randint(3, 14)), total)
    return sorted(today - timedelta(days=offset) for offset in offsets)


def _misses(
    rng: random.Random, words: list[str], answers: set[str], count: int,
) -> list[str]:
    """Подбирает разные допустимые ходы, не раскрывающие ответы."""
    return rng.sample([word for word in words if word not in answers], count)


def make_wordli_profile(
    engine: LocalGameEngine, rng: random.Random, user_id: int, today: date,
) -> dict[str, Any]:
    """Возвращает поля игрового профиля 5БУКВ без записи в БД."""
    days = _days(rng, today)
    answers = rng.sample(engine._answers, len(days))
    words = sorted(engine._words)
    wins = rng.randint(len(days) // 2, len(days) - 2)
    winning_days = set(rng.sample(days, wins))
    history = []
    revision = 0
    for day, answer in zip(days, answers):
        won = day in winning_days
        if won:
            turns = rng.choices(range(1, 7), (1, 4, 12, 16, 11, 5))[0]
        else:
            turns = WORDLI_ATTEMPTS
        guesses = _misses(rng, words, {answer}, turns - int(won))
        if won:
            guesses.append(answer)
        day_key = day.isoformat()
        # Закрепляем ответ: серверный secrets.choice не использует наш seed.
        current = [{
            "day": day_key, "index": 0, "draft": "", "guesses": [],
            "answer": answer, "dictionary_id": engine.dictionary_id,
        }]
        for word in guesses:
            state = engine.submit(
                current, day_key, word, today=day, player_id=user_id
            )
            current = state["history"]
        history.extend(current)
        revision += 1 + len(guesses)
    # Проверяем всю историю штатным пересчётом без выдачи слова на сегодня.
    if engine.statistics(history, today)["played"] != len(days):
        raise ValueError("История 5БУКВ содержит незавершённую партию")
    return {
        "history": history, "dictionary_id": engine.dictionary_id,
        "revision": revision,
    }


def make_quordle_profile(
    engine: QuordleEngine, rng: random.Random, today: date,
) -> dict[str, Any]:
    """Возвращает личный профиль КВОРДЛИ с колодой и историей всех ходов."""
    days = _days(rng, today)
    losses = rng.randint(4, max(4, len(days) * 3 // 5))
    # Каждая демоистория содержит все четыре типа частичного результата.
    solved = list(range(BOARD_COUNT))
    solved.extend(rng.choices(range(BOARD_COUNT), k=losses - len(solved)))
    solved.extend([BOARD_COUNT] * (len(days) - losses))
    rng.shuffle(solved)
    # Штатная выдача и смена циклов, но RNG только данного тестового запуска.
    simulator = copy(engine)
    simulator._rng = rng
    words = sorted(simulator._words)
    profile: dict[str, Any] = {}
    for day, found in zip(days, solved):
        simulator.bootstrap(profile, day)
        answers = profile["history"][-1]["answers"]
        ordered = rng.sample(answers, found)
        won = found == BOARD_COUNT
        if won:
            turns = rng.choices(range(4, 10), (1, 4, 11, 15, 10, 6))[0]
        else:
            turns = QUORDLE_ATTEMPTS
        guesses = _misses(rng, words, set(answers), turns - found)
        guesses.extend(ordered[:-1] if won else ordered)
        rng.shuffle(guesses)
        if won:
            guesses.append(ordered[-1])
        for word in guesses:
            simulator.submit(profile, word, day)
    if engine.stats(profile, today)["played"] != len(days):
        raise ValueError("История КВОРДЛИ содержит незавершённую партию")
    return profile
