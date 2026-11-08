"""Проверки новых весов очков и позиционного черновика без сети."""

from datetime import date
from math import floor, sqrt

import pytest

from prototypes.wordli.engine import (
    GameError,
    LocalGameEngine,
    calculate_score,
    normalize_draft,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (" А К", " А К"),
        ("ко ка", "КО КА"),
        ("  ё  ", "  Е"),
        ("     ", ""),
        ("ПАРОМ", "ПАРОМ"),
    ],
)
def test_draft_preserves_positions(raw: str, expected: str) -> None:
    """Ведущие и внутренние пробелы сохраняют пустые ячейки."""
    assert normalize_draft(raw) == expected


@pytest.mark.parametrize("raw", ["\tА", "А\n", "А-Б", "AБ", "АБВГДЕ", 10, None])
def test_draft_rejects_other_characters(raw: object) -> None:
    """Служебные символы и неверный алфавит не попадают в поле."""
    with pytest.raises(GameError, match="строка ввода"):
        normalize_draft(raw)


def test_sparse_draft_roundtrip_does_not_submit_guess() -> None:
    """Пустые позиции переживают восстановление и не расходуют попытку."""
    rules = LocalGameEngine()
    today = date(2026, 11, 8)
    state = rules.bootstrap([], today, player_id=9001)
    history = state["history"]
    history[-1]["draft"] = " А К"
    restored = rules.bootstrap(history, today, player_id=9001)
    assert restored["game"]["draft"] == " А К"
    assert restored["game"]["guesses"] == []
    with pytest.raises(GameError, match="пяти русских букв"):
        rules.submit(history, today.isoformat(), " А К", today, player_id=9001)


def test_statistics_does_not_allocate_new_game() -> None:
    """Таблица лидеров не выдаёт и не расходует слово игрока."""
    rules = LocalGameEngine()
    today = date(2026, 11, 8)
    assert rules.statistics([], today)["played"] == 0
    state = rules.bootstrap([], today)
    assert len(state["history"]) == 1
    assert (
        rules.statistics(state["history"], today)["score_version"]
        == "progress-quality-v2"
    )


@pytest.mark.parametrize(
    "played,wins,average,streak,pool",
    [(10, 7, 3.5, 4, 1758), (100, 95, 2.5, 30, 1758), (5000, 4000, 6, 100, 5000)],
)
def test_v2_weights_match_approved_formula(
    played: int, wins: int, average: float, streak: int, pool: int
) -> None:
    """Сервер применяет веса 0,4/0,4/0,2, а не прежнее описание."""
    expected = floor(
        10000
        * sqrt(played / pool)
        * (wins / played)
        * (0.4 + 0.4 * (7 - average) / 6 + 0.2 * sqrt(streak / pool))
    )
    assert calculate_score(played, wins, average, streak, pool) == expected
