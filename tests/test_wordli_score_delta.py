"""Изменение очков за последнюю завершённую игру без сети и рабочих данных."""

import copy
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from prototypes.wordli.engine import LocalGameEngine

TODAY = date(2026, 11, 12)
DICTIONARY_ID = "score-delta-unit-tests"
WORDS = ["БАНАН", "БИЛЕТ", "ВАТКА", "ДОСКА", "КОМАР", "КОШКА", "ПАРОМ", "ТАЧКА"]


@pytest.fixture
def rules(tmp_path: Path) -> LocalGameEngine:
    """Загружает восемь синтетических ответов в отдельный движок."""
    filename = tmp_path / "score-delta-dictionary.json"
    filename.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dictionary_id": DICTIONARY_ID,
                "normalization": "Ё=Е",
                "words": WORDS,
                "answers": WORDS,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return LocalGameEngine(filename)


def _history(
    results: list[int],
    *,
    last_day: date = TODAY,
    same_day: bool = False,
) -> list[dict[str, Any]]:
    """Создаёт неповторяющиеся ответы; ноль означает поражение."""
    records = []
    for index, attempts in enumerate(results):
        answer = WORDS[index]
        wrong = next(word for word in WORDS if word != answer)
        day = last_day if same_day else last_day - timedelta(
            days=len(results) - index - 1
        )
        guesses = [wrong] * (attempts - 1) + [answer] if attempts else [wrong] * 6
        records.append(
            {
                "day": day.isoformat(),
                "index": index if same_day else 0,
                "answer": answer,
                "dictionary_id": DICTIONARY_ID,
                "guesses": guesses,
                "draft": "",
            }
        )
    return records


@pytest.mark.parametrize("played", range(6))
def test_delta_hidden_before_rating_and_on_first_score(
    rules: LocalGameEngine, played: int,
) -> None:
    """Первое начисление не сравнивается с условным нулём."""
    stats = rules.statistics(_history([1] * played), TODAY)
    assert stats["score_delta"] is None
    assert (stats["score"] is None) == (played < 5)


@pytest.mark.parametrize(
    "results",
    [
        [1, 1, 1, 1, 1, 1],
        [1, 1, 1, 1, 1, 0],
        [0, 0, 0, 0, 0, 1],
        [1, 2, 3, 4, 5, 6],
        [1, 1, 0, 1, 1, 1],
        [1, 1, 1, 0, 1, 0],
        [1, 0, 2, 3, 0, 4, 1],
    ],
)
def test_delta_is_difference_of_two_integer_scores(
    rules: LocalGameEngine, results: list[int],
) -> None:
    """Победы, поражения, среднее и максимум серии входят в обе оценки."""
    history = _history(results)
    current = rules.statistics(history, TODAY)
    previous = rules.statistics(history[:-1], TODAY)
    expected = current["score"] - previous["score"]
    assert current["score_delta"] == (expected if expected else None)
    if expected:
        assert type(current["score_delta"]) is int


def test_win_adds_points_and_loss_can_remove_points(rules: LocalGameEngine) -> None:
    """Знак изменения соответствует результату последней игры."""
    won = rules.statistics(_history([1] * 6), TODAY)
    lost = rules.statistics(_history([1] * 5 + [0]), TODAY)
    assert won["score_delta"] > 0
    assert lost["score_delta"] < 0


@pytest.mark.parametrize("played", [5, 6, 7, 8])
def test_zero_delta_is_hidden(rules: LocalGameEngine, played: int) -> None:
    """Нулевая оценка без побед не создаёт подпись с нулевым изменением."""
    stats = rules.statistics(_history([0] * played), TODAY)
    assert stats["score"] == 0
    assert stats["score_delta"] is None


def test_first_win_after_qualification_compares_with_real_zero(
    rules: LocalGameEngine,
) -> None:
    """Нулевые очки после пяти поражений — число, а не отсутствие оценки."""
    stats = rules.statistics(_history([0] * 5 + [1]), TODAY)
    assert stats["score"] > 0
    assert stats["score_delta"] == stats["score"]


def test_expired_and_unfinished_games_do_not_replace_last_delta(
    rules: LocalGameEngine,
) -> None:
    """Просрочка и текущий черновик не считаются последней завершённой игрой."""
    completed = _history([1, 2, 3, 4, 5, 1], last_day=TODAY - timedelta(days=2))
    expected = rules.statistics(completed, TODAY)
    history = completed + [
        {
            "day": (TODAY - timedelta(days=1)).isoformat(),
            "answer": WORDS[6],
            "dictionary_id": DICTIONARY_ID,
            "guesses": [WORDS[0]],
            "draft": "ТА",
        },
        {
            "day": TODAY.isoformat(),
            "answer": WORDS[7],
            "dictionary_id": DICTIONARY_ID,
            "guesses": [],
            "draft": " КО",
        },
    ]
    original = copy.deepcopy(history)
    for day in [TODAY, TODAY + timedelta(days=7)]:
        actual = rules.statistics(history, day)
        assert actual["played"] == 6
        assert actual["score"] == expected["score"]
        assert actual["score_delta"] == expected["score_delta"]
    assert history == original


def test_same_day_order_and_reopening_keep_delta(rules: LocalGameEngine) -> None:
    """Порядок браузерного списка и повторное открытие не сбрасывают изменение."""
    history = _history([1, 2, 3, 4, 5, 0], same_day=True)
    expected = rules.statistics(history, TODAY)
    unsorted = list(reversed(history))
    original = copy.deepcopy(unsorted)
    for _ in range(2):
        assert rules.statistics(unsorted, TODAY) == expected
        assert rules.bootstrap(unsorted, TODAY)["stats"] == expected
    assert unsorted == original


def test_new_daily_word_does_not_reset_previous_delta(rules: LocalGameEngine) -> None:
    """Выдача следующего дневного поля не обнуляет последнее изменение очков."""
    history = _history([1, 2, 3, 4, 5, 0], last_day=TODAY - timedelta(days=1))
    original = copy.deepcopy(history)
    expected = rules.statistics(history, TODAY)
    state = rules.bootstrap(history, TODAY, player_id=900001)
    assert state["game"]["status"] == "in_progress"
    assert state["game"]["guesses"] == []
    assert state["stats"] == expected
    assert history == original


def test_forged_history_delta_is_ignored(rules: LocalGameEngine) -> None:
    """Сохранённый браузером прирост не заменяет серверный расчёт."""
    history = _history([1] * 6)
    expected = rules.statistics(history, TODAY)
    history[-1].update(score=10000, score_delta=999999)
    assert rules.statistics(history, TODAY) == expected
