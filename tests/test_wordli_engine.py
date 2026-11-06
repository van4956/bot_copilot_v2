"""Проверки настоящих правил Wordli на отдельном маленьком словаре."""

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from prototypes.wordli.engine import (
    MAX_GAMES_PER_DAY,
    MAX_HISTORY_DAYS,
    GameError,
    LocalGameEngine,
    calculate_score,
    evaluate_guess,
    normalize_word,
)

WORDS = [
    "БАНАН",
    "БИЛЕТ",
    "ВАТКА",
    "ДОСКА",
    "КОМАР",
    "КОШКА",
    "ПАРОМ",
    "ТАЧКА",
]
TODAY = date(2026, 11, 5)


def test_extra_a_is_gray_when_only_last_a_exists() -> None:
    """Точное совпадение забирает единственную А раньше лишнего повтора."""
    states = evaluate_guess("БУЛКА", "СВАХА")
    assert states[2] == "absent"
    assert states[4] == "correct"


def test_extra_a_is_white_when_answer_has_two_a_letters() -> None:
    """Вторая А белая только при наличии ещё одной А в другом месте."""
    states = evaluate_guess("ЛАПКА", "СВАХА")
    assert states[2] == "present"
    assert states[4] == "correct"


@pytest.fixture
def engine(tmp_path: Path) -> LocalGameEngine:
    """Загружает тестовый словарь без боевых данных и сетевых запросов."""
    filename = tmp_path / "words.json"
    filename.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dictionary_id": "unit-test-ru5",
                "normalization": "Ё=Е",
                "words": WORDS,
                "answers": WORDS,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return LocalGameEngine(filename)


def _wrong(engine: LocalGameEngine, day: date) -> str:
    """Выбирает допустимое слово, не являющееся ответом указанного дня."""
    return next(word for word in WORDS if word != engine.word_for_day(day))


def _win(engine: LocalGameEngine, day: date, attempts: int = 1) -> dict:
    """Собирает проверяемую запись победной партии."""
    return {
        "day": day.isoformat(),
        "guesses": [_wrong(engine, day)] * (attempts - 1) + [engine.word_for_day(day)],
        "draft": "",
    }


def _loss(engine: LocalGameEngine, day: date) -> dict:
    """Собирает запись проигрыша после шести допустимых ходов."""
    return {"day": day.isoformat(), "guesses": [_wrong(engine, day)] * 6}


@pytest.mark.parametrize(
    "raw",
    [" ёлочка ", "12345", "ABВГД", "КОШК", "КОШКА!", "КО ШК", "", None, 1],
)
def test_invalid_word_format(raw: object) -> None:
    """Слова неверной длины, алфавита и типа не проходят нормализацию."""
    with pytest.raises(GameError) as error:
        normalize_word(raw)
    assert error.value.code == "invalid_word"


def test_word_normalization() -> None:
    """Регистр, пробелы по краям и составная Ё приводятся одинаково."""
    assert normalize_word(" кошка ") == "КОШКА"
    assert normalize_word("бе\u0308дра") == "БЕДРА"


@pytest.mark.parametrize(
    "answer,guess,expected",
    [
        (
            "КОМАР",
            "КОШКА",
            ["correct", "correct", "absent", "absent", "present"],
        ),
        (
            "БАНАН",
            "НАБАТ",
            ["present", "correct", "present", "correct", "absent"],
        ),
        ("КОШКА", "КОШКА", ["correct"] * 5),
        ("ПАРОМ", "БИЛЕТ", ["absent"] * 5),
    ],
)
def test_letter_allocation(answer: str, guess: str, expected: list[str]) -> None:
    """Повторы не получают больше отметок, чем букв есть в ответе."""
    assert evaluate_guess(answer, guess) == expected


def test_empty_game_does_not_reveal_answer(engine: LocalGameEngine) -> None:
    """Новая партия пуста, исход не придуман, ответ скрыт."""
    state = engine.bootstrap([], TODAY)
    assert state["day"] == TODAY.isoformat()
    assert state["expires_at"] == "2026-11-06T00:00:00Z"
    assert state["game"]["guesses"] == []
    assert state["game"]["outcome"] is None
    assert state["game"]["answer"] is None
    assert state["game"]["status"] == "in_progress"
    assert state["stats"]["played"] == 0
    assert state["stats"]["average"] is None
    assert state["dictionary"] == {
        "id": "unit-test-ru5",
        "size": len(WORDS),
        "answers": len(WORDS),
    }


def test_daily_schedule_is_stable_and_has_no_cycle_repeats(
    engine: LocalGameEngine, tmp_path: Path
) -> None:
    """Порядок слов JSON и перезапуск процесса не меняют сегодняшнее слово."""
    filename = tmp_path / "reversed.json"
    filename.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dictionary_id": "unit-test-ru5",
                "normalization": "Ё=Е",
                "words": list(reversed(WORDS)),
                "answers": list(reversed(WORDS)),
            }
        ),
        encoding="utf-8",
    )
    other = LocalGameEngine(filename)
    days = [TODAY + timedelta(days=index) for index in range(len(WORDS))]
    assert len({engine.word_for_day(day) for day in days}) == len(WORDS)
    assert [engine.word_for_day(day) for day in days] == [
        other.word_for_day(day) for day in days
    ]
    assert engine.word_for_day(TODAY) == engine.word_for_day(
        TODAY + timedelta(days=len(WORDS))
    )


def test_valid_guess_persists_as_canonical_history_without_mutation(
    engine: LocalGameEngine,
) -> None:
    """Принятый ход очищает черновик, но исходный список не меняет."""
    history = [{"day": TODAY.isoformat(), "guesses": [], "draft": "пар"}]
    original = copy.deepcopy(history)
    result = engine.submit(
        history, TODAY.isoformat(), _wrong(engine, TODAY).lower(), TODAY
    )
    assert history == original
    assert result["game"]["guesses"] == [_wrong(engine, TODAY)]
    assert result["game"]["draft"] == ""
    assert len(result["game"]["rows"]) == 1
    assert result["game"]["answer"] is None
    assert result["stats"]["played"] == 0
    assert engine.bootstrap(result["history"], TODAY) == result


@pytest.mark.parametrize(
    "guess,code", [("КОШК", "invalid_word"), ("АБВГД", "unknown_word")]
)
def test_rejected_guess_does_not_spend_attempt(
    engine: LocalGameEngine, guess: str, code: str
) -> None:
    """Неверный новый ход не изменяет историю и число попыток."""
    history = [{"day": TODAY.isoformat(), "guesses": [_wrong(engine, TODAY)]}]
    original = copy.deepcopy(history)
    with pytest.raises(GameError) as error:
        engine.submit(history, TODAY.isoformat(), guess, TODAY)
    assert error.value.code == code
    assert history == original
    assert len(engine.bootstrap(history, TODAY)["game"]["guesses"]) == 1


@pytest.mark.parametrize("attempts", [1, 6])
def test_win_on_first_or_last_attempt(engine: LocalGameEngine, attempts: int) -> None:
    """Правильное слово выигрывает и на последнем разрешённом ходе."""
    history = [
        {
            "day": TODAY.isoformat(),
            "guesses": [_wrong(engine, TODAY)] * (attempts - 1),
        }
    ]
    result = engine.submit(
        history, TODAY.isoformat(), engine.word_for_day(TODAY), TODAY
    )
    assert result["game"]["status"] == "won"
    assert result["game"]["outcome"] == "win"
    assert result["game"]["answer"] == engine.word_for_day(TODAY)
    assert result["stats"]["distribution"][attempts - 1] == 1
    assert result["stats"]["average"] == attempts
    with pytest.raises(GameError) as error:
        engine.submit(
            result["history"], TODAY.isoformat(), _wrong(engine, TODAY), TODAY
        )
    assert error.value.code == "game_finished"
    assert error.value.status == 409


def test_six_wrong_guesses_are_a_loss(engine: LocalGameEngine) -> None:
    """Преднамеренные повторы допустимы, шестой промах завершает игру."""
    history = [{"day": TODAY.isoformat(), "guesses": [_wrong(engine, TODAY)] * 5}]
    result = engine.submit(history, TODAY.isoformat(), _wrong(engine, TODAY), TODAY)
    assert result["game"]["status"] == "lost"
    assert result["game"]["answer"] == engine.word_for_day(TODAY)
    assert result["stats"]["played"] == 1
    assert result["stats"]["wins"] == 0
    assert result["stats"]["average"] is None
    assert result["stats"]["strict_streak"] == 0
    with pytest.raises(GameError) as error:
        engine.submit(
            result["history"],
            TODAY.isoformat(),
            engine.word_for_day(TODAY),
            TODAY,
        )
    assert error.value.code == "game_finished"


def test_draft_restores_and_normalizes(engine: LocalGameEngine) -> None:
    """Незавершённая строка восстанавливается без фиктивной попытки."""
    state = engine.bootstrap([{"day": TODAY.isoformat(), "draft": "бе\u0308д"}], TODAY)
    assert state["game"]["draft"] == "БЕД"
    assert state["game"]["rows"] == []


def test_day_rollover_does_not_record_a_loss(engine: LocalGameEngine) -> None:
    """Просроченные пять попыток не прибавляют поражение."""
    yesterday = TODAY - timedelta(days=1)
    history = [
        {
            "day": yesterday.isoformat(),
            "guesses": [_wrong(engine, yesterday)] * 5,
        }
    ]
    result = engine.bootstrap(history, TODAY)
    assert result["game"]["guesses"] == []
    assert result["stats"]["played"] == 0
    assert result["stats"]["wins"] == 0
    assert len(result["history"]) == 2
    with pytest.raises(GameError) as error:
        engine.submit(history, yesterday.isoformat(), _wrong(engine, yesterday), TODAY)
    assert error.value.code == "day_changed"
    assert error.value.status == 409


def test_streaks_differ_after_a_missed_day(engine: LocalGameEngine) -> None:
    """Пропуск обрывает только календарную непрерывную серию."""
    history = [_win(engine, TODAY - timedelta(days=2)), _win(engine, TODAY, 3)]
    stats = engine.bootstrap(history, TODAY)["stats"]
    assert stats["win_streak"] == 2
    assert stats["strict_streak"] == 1
    assert stats["max_streak"] == 2
    assert stats["played"] == 2
    assert stats["average"] == 2
    assert stats["win_rate"] == 100
    assert stats["distribution"] == [1, 0, 1, 0, 0, 0]


def test_current_unfinished_day_keeps_yesterdays_streak(
    engine: LocalGameEngine,
) -> None:
    """Текущие незавершённые сутки не считаются пропущенными заранее."""
    history = [
        _win(engine, TODAY - timedelta(days=2)),
        _win(engine, TODAY - timedelta(days=1)),
    ]
    assert engine.bootstrap(history, TODAY)["stats"]["strict_streak"] == 2
    assert (
        engine.bootstrap(history, TODAY + timedelta(days=1))["stats"]["strict_streak"]
        == 0
    )
    assert (
        engine.bootstrap(history, TODAY + timedelta(days=1))["stats"]["win_streak"] == 2
    )


def test_loss_resets_both_series_but_keeps_maximum(
    engine: LocalGameEngine,
) -> None:
    """Проигрыш обнуляет текущие серии, но не исторический рекорд."""
    history = [
        _win(engine, TODAY - timedelta(days=2)),
        _win(engine, TODAY - timedelta(days=1)),
        _loss(engine, TODAY),
    ]
    stats = engine.bootstrap(history, TODAY)["stats"]
    assert stats["played"] == 3
    assert stats["wins"] == 2
    assert stats["win_streak"] == 0
    assert stats["strict_streak"] == 0
    assert stats["max_streak"] == 2
    assert stats["win_rate"] == pytest.approx(200 / 3)


def test_statistics_reconstruct_from_attempts_not_claimed_scores(
    engine: LocalGameEngine,
) -> None:
    """Сохранённые поля outcome и score не подменяют реальные исходы ходов."""
    record = _loss(engine, TODAY)
    record.update({"outcome": "win", "score": 999999})
    assert engine.bootstrap([record], TODAY)["stats"]["wins"] == 0


@pytest.mark.parametrize(
    "history",
    [
        None,
        {},
        [None],
        [{"day": "20261105"}],
        [{"day": "2026-02-30"}],
        [{"day": "2026-11-06"}],
        [{"day": "2026-11-05"}, {"day": "2026-11-05"}],
        [{"day": "2026-11-05", "guesses": "КОШКА"}],
        [{"day": "2026-11-05", "guesses": [1]}],
        [{"day": "2026-11-05", "guesses": ["АБВГД"]}],
        [{"day": "2026-11-05", "guesses": ["КОШКА"] * 7}],
        [{"day": "2026-11-05", "draft": None}],
        [{"day": "2026-11-05", "draft": "123"}],
        [{"day": "2026-11-05", "draft": "КОШКАА"}],
    ],
)
def test_malformed_history_is_rejected(
    engine: LocalGameEngine, history: object
) -> None:
    """Повреждённое сохранение не перезаписывается молча пустой партией."""
    with pytest.raises(GameError) as error:
        engine.bootstrap(history, TODAY)
    assert error.value.code == "invalid_history"


def test_history_cannot_add_guesses_after_a_win(
    engine: LocalGameEngine,
) -> None:
    """Поддельные последующие ходы после правильного слова отклоняются."""
    record = _win(engine, TODAY)
    record["guesses"].append(_wrong(engine, TODAY))
    with pytest.raises(GameError) as error:
        engine.bootstrap([record], TODAY)
    assert error.value.code == "invalid_history"


def test_keyboard_never_downgrades_a_hint(engine: LocalGameEngine) -> None:
    """Последующий серый повтор не уничтожает найденную букву клавиатуры."""
    today = next(
        TODAY + timedelta(days=index)
        for index in range(len(WORDS))
        if engine.word_for_day(TODAY + timedelta(days=index)) == "КОМАР"
    )
    result = engine.bootstrap(
        [{"day": today.isoformat(), "guesses": ["КОШКА", "ВАТКА"]}], today
    )
    assert result["game"]["keyboard"]["К"] == "correct"
    assert result["game"]["keyboard"]["А"] == "present"


def test_history_limit_is_bounded(engine: LocalGameEngine) -> None:
    """Размер истории ограничен до обработки недоверенного списка."""
    with pytest.raises(GameError) as error:
        engine.bootstrap([{}] * (MAX_HISTORY_DAYS + 1), TODAY)
    assert error.value.code == "invalid_history"


@pytest.mark.parametrize(
    "today,expected_end",
    [
        (date(2026, 12, 31), "2027-01-01T00:00:00Z"),
        (date(2028, 2, 28), "2028-02-29T00:00:00Z"),
        (date(2028, 2, 29), "2028-03-01T00:00:00Z"),
    ],
)
def test_calendar_boundaries(
    engine: LocalGameEngine, today: date, expected_end: str
) -> None:
    """Окончание дня учитывает месяц, год и високосный февраль."""
    assert engine.bootstrap([], today)["expires_at"] == expected_end


def test_unsorted_history_is_ordered_before_statistics(
    engine: LocalGameEngine,
) -> None:
    """Порядок записей браузера не меняет календарную историю результатов."""
    history = [
        _win(engine, TODAY),
        _loss(engine, TODAY - timedelta(days=1)),
        _win(engine, TODAY - timedelta(days=2)),
    ]
    state = engine.bootstrap(history, TODAY)
    assert [record["day"] for record in state["history"]] == sorted(
        record["day"] for record in history
    )
    assert state["stats"]["win_streak"] == 1
    assert state["stats"]["strict_streak"] == 1
    assert state["stats"]["max_streak"] == 1


def test_losses_do_not_inflate_average_winning_attempts(
    engine: LocalGameEngine,
) -> None:
    """Шесть проигранных попыток не увеличивают среднее победных ходов."""
    history = [_loss(engine, TODAY - timedelta(days=1)), _win(engine, TODAY)]
    stats = engine.bootstrap(history, TODAY)["stats"]
    assert stats["average"] == 1
    assert stats["win_rate"] == 50
    assert stats["distribution"] == [1, 0, 0, 0, 0, 0]


def test_partial_expired_game_breaks_only_calendar_streak(
    engine: LocalGameEngine,
) -> None:
    """Незавершённый пропущенный день не ломает обычную серию побед."""
    yesterday = TODAY - timedelta(days=1)
    history = [
        _win(engine, TODAY - timedelta(days=2)),
        {"day": yesterday.isoformat(), "guesses": [_wrong(engine, yesterday)]},
        _win(engine, TODAY),
    ]
    stats = engine.bootstrap(history, TODAY)["stats"]
    assert stats["played"] == 2
    assert stats["wins"] == 2
    assert stats["strict_streak"] == 1
    assert stats["win_streak"] == 2


@pytest.mark.parametrize(
    "words,answers",
    [
        ([], []),
        (["КОШКА", "КОШКА"], ["КОШКА"]),
        (["кошка"], ["кошка"]),
        (["КОШКА"], ["ПАРОМ"]),
        (["КОШКА"], []),
    ],
)
def test_invalid_dictionary_fails_at_startup(
    tmp_path: Path, words: list[str], answers: list[str]
) -> None:
    """Дефектный словарь не превращается в пустую рабочую игру."""
    filename = tmp_path / "bad.json"
    filename.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dictionary_id": "bad",
                "normalization": "Ё=Е",
                "words": words,
                "answers": answers,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        LocalGameEngine(filename)


def _indexed_win(engine: LocalGameEngine, day: date, index: int) -> dict:
    """Собирает победу конкретной дополнительной партии."""
    return {
        "day": day.isoformat(),
        "index": index,
        "guesses": [engine.word_for_game(day, index)],
        "draft": "",
    }


def _indexed_loss(engine: LocalGameEngine, day: date, index: int) -> dict:
    """Собирает проигрыш конкретной дополнительной партии."""
    wrong = next(word for word in WORDS if word != engine.word_for_game(day, index))
    return {
        "day": day.isoformat(),
        "index": index,
        "guesses": [wrong] * 6,
        "draft": "",
    }


def test_old_history_migrates_to_index_zero_without_loss(
    engine: LocalGameEngine,
) -> None:
    """Первая версия сохранения не теряет ходы и календарные результаты."""
    old = [
        _win(engine, TODAY - timedelta(days=1)),
        {"day": TODAY.isoformat(), "guesses": [], "draft": "ТА"},
    ]
    original = copy.deepcopy(old)
    result = engine.bootstrap(old, TODAY)
    assert old == original
    assert all(record["index"] == 0 for record in result["history"])
    assert result["game"]["id"] == "2026-11-05:0"
    assert result["game"]["draft"] == "ТА"
    assert result["stats"]["wins"] == 1
    assert result["stats"]["strict_streak"] == 1


def test_additional_words_never_repeat_daily_within_extra_cycles(
    engine: LocalGameEngine,
) -> None:
    """Дополнительный цикл не выдаёт дневной ответ даже после повторения."""
    size = len(WORDS)
    words = [engine.word_for_game(TODAY, index) for index in range(size)]
    assert words[0] == engine.word_for_day(TODAY)
    assert len(set(words)) == size
    second_cycle = [
        engine.word_for_game(TODAY, index) for index in range(size, size * 2 - 1)
    ]
    assert second_cycle == words[1:]
    assert words[0] not in second_cycle


@pytest.mark.parametrize("finished", ["win", "loss"])
def test_next_game_starts_only_after_current_result(
    engine: LocalGameEngine, finished: str
) -> None:
    """После победы или поражения появляется новое пустое поле."""
    record = (
        _indexed_win(engine, TODAY, 0)
        if finished == "win"
        else _indexed_loss(engine, TODAY, 0)
    )
    original = copy.deepcopy(record)
    result = engine.next_game([record], TODAY.isoformat(), 0, TODAY)
    assert record == original
    assert result["game"]["index"] == 1
    assert result["game"]["id"] == "2026-11-05:1"
    assert result["game"]["guesses"] == []
    assert result["game"]["answer"] is None
    assert result["game"]["outcome"] is None
    assert result["stats"]["played"] == 1
    assert all(result["history"][0][key] == value for key, value in original.items())
    assert result["history"][0]["answer"] == engine.word_for_game(TODAY, 0)


def test_next_game_cannot_skip_an_unfinished_round(
    engine: LocalGameEngine,
) -> None:
    """Запрос следующего слова не списывает незаконченный результат."""
    with pytest.raises(GameError) as error:
        engine.next_game([], TODAY.isoformat(), 0, TODAY)
    assert error.value.code == "game_in_progress"
    assert error.value.status == 409


def test_bootstrap_resumes_latest_index_with_its_draft(
    engine: LocalGameEngine,
) -> None:
    """Перезагрузка восстанавливает последнюю партию, а не слово дня."""
    history = [
        _indexed_win(engine, TODAY, 0),
        {"day": TODAY.isoformat(), "index": 1, "draft": "КОШ"},
    ]
    result = engine.bootstrap(list(reversed(history)), TODAY)
    assert [record["index"] for record in result["history"]] == [0, 1]
    assert result["game"]["index"] == 1
    assert result["game"]["draft"] == "КОШ"
    assert result["stats"]["wins"] == 1


def test_submit_checks_active_index_and_correct_extra_answer(
    engine: LocalGameEngine,
) -> None:
    """Старая вкладка не отправляет ход в другую активную партию."""
    state = engine.next_game(
        [_indexed_win(engine, TODAY, 0)], TODAY.isoformat(), 0, TODAY
    )
    for stale in [0, 2]:
        with pytest.raises(GameError) as error:
            engine.submit(
                state["history"],
                TODAY.isoformat(),
                "КОШКА",
                TODAY,
                index=stale,
            )
        assert error.value.code == "game_changed"
        assert error.value.status == 409
    result = engine.submit(
        state["history"],
        TODAY.isoformat(),
        engine.word_for_game(TODAY, 1),
        TODAY,
        index=1,
    )
    assert result["game"]["outcome"] == "win"
    assert result["game"]["answer"] == engine.word_for_game(TODAY, 1)
    assert result["stats"]["wins"] == 2
    assert result["stats"]["strict_streak"] == 2
    assert result["stats"]["win_streak"] == 2


def test_double_next_request_does_not_skip_two_words(
    engine: LocalGameEngine,
) -> None:
    """Повтор запроса возвращает ту же партию либо актуальный конфликт."""
    history = [_indexed_win(engine, TODAY, 0)]
    first = engine.next_game(history, TODAY.isoformat(), 0, TODAY)
    assert engine.next_game(history, TODAY.isoformat(), 0, TODAY) == first
    with pytest.raises(GameError) as error:
        engine.next_game(first["history"], TODAY.isoformat(), 0, TODAY)
    assert error.value.code == "game_changed"


@pytest.mark.parametrize("total", [0, 1, 4, 5, 6, 10, 11])
def test_five_win_progression_windows(engine: LocalGameEngine, total: int) -> None:
    """Окно сдвигается после шестой победы, а не после пятой."""
    history = [_indexed_win(engine, TODAY, index) for index in range(total)]
    result = engine.bootstrap(history, TODAY)
    progression = result["progression"]
    expected_start = ((total - 1) // 5) * 5 + 1 if total else 1
    assert progression["total_wins"] == total
    assert progression["start"] == expected_start
    assert progression["end"] == expected_start + 4
    assert [node["number"] for node in progression["nodes"]] == list(
        range(expected_start, expected_start + 5)
    )
    for node in progression["nodes"]:
        assert node["complete"] == (node["number"] <= total)
        if node["complete"]:
            assert node["word"] == engine.word_for_game(TODAY, node["number"] - 1)
        else:
            assert node["word"] is None


def test_progression_counts_only_wins_across_losses_and_days(
    engine: LocalGameEngine,
) -> None:
    """Проигранное и незавершённое слово не раскрывает следующий узел."""
    yesterday = TODAY - timedelta(days=1)
    history = [
        _indexed_win(engine, yesterday, 0),
        _indexed_loss(engine, yesterday, 1),
        _indexed_win(engine, TODAY, 0),
        {"day": TODAY.isoformat(), "index": 1, "draft": "КО"},
    ]
    result = engine.bootstrap(history, TODAY)
    assert result["progression"]["total_wins"] == 2
    assert [node["complete"] for node in result["progression"]["nodes"]] == [
        True,
        True,
        False,
        False,
        False,
    ]
    assert result["stats"]["played"] == 3
    assert result["stats"]["strict_streak"] == 1
    assert result["stats"]["win_streak"] == 1


def test_strict_streak_counts_words_and_resets_after_calendar_gap(
    engine: LocalGameEngine,
) -> None:
    """Все победы дня учитываются, пропуск обрывает строгую серию."""
    before = TODAY - timedelta(days=2)
    history = [
        _indexed_win(engine, before, 0),
        _indexed_win(engine, before, 1),
        _indexed_win(engine, TODAY, 0),
        _indexed_win(engine, TODAY, 1),
    ]
    stats = engine.bootstrap(history, TODAY)["stats"]
    assert stats["strict_streak"] == 2
    assert stats["win_streak"] == 4
    assert stats["max_streak"] == 4


def test_yesterday_multiple_wins_are_retained_until_today_expires(
    engine: LocalGameEngine,
) -> None:
    """Незаконченный текущий день сохраняет вчерашнюю серию слов."""
    yesterday = TODAY - timedelta(days=1)
    history = [_indexed_win(engine, yesterday, index) for index in range(3)]
    assert engine.bootstrap(history, TODAY)["stats"]["strict_streak"] == 3
    stats = engine.bootstrap(history, TODAY + timedelta(days=1))["stats"]
    assert stats["strict_streak"] == 0
    assert stats["win_streak"] == 3


def test_extra_loss_resets_both_word_streaks(
    engine: LocalGameEngine,
) -> None:
    """Проигрыш между победами одного дня сбрасывает обе серии."""
    history = [
        _indexed_win(engine, TODAY, 0),
        _indexed_win(engine, TODAY, 1),
        _indexed_loss(engine, TODAY, 2),
        _indexed_win(engine, TODAY, 3),
    ]
    stats = engine.bootstrap(history, TODAY)["stats"]
    assert stats["strict_streak"] == 1
    assert stats["win_streak"] == 1
    assert stats["max_streak"] == 2


@pytest.mark.parametrize("invalid", [-1, True, 1.0, "1", None, 1000])
def test_invalid_replay_indexes_are_rejected(
    engine: LocalGameEngine, invalid: object
) -> None:
    """Булевы, дробные и чрезмерные номера не становятся партиями."""
    with pytest.raises(GameError) as error:
        engine.next_game([], TODAY.isoformat(), invalid, TODAY)
    assert error.value.code == "invalid_index"


@pytest.mark.parametrize("indexes", [[1], [0, 2], [0, 0]])
def test_history_cannot_skip_or_duplicate_an_index(
    engine: LocalGameEngine, indexes: list[int]
) -> None:
    """История содержит только непрерывную последовательность партий дня."""
    records = [_indexed_win(engine, TODAY, index) for index in indexes]
    with pytest.raises(GameError) as error:
        engine.bootstrap(records, TODAY)
    assert error.value.code == "invalid_history"


def test_history_cannot_skip_an_unfinished_index(
    engine: LocalGameEngine,
) -> None:
    """Нельзя добавить новую партию после неполной предыдущей истории."""
    history = [
        {"day": TODAY.isoformat(), "index": 0, "guesses": []},
        _indexed_win(engine, TODAY, 1),
    ]
    with pytest.raises(GameError) as error:
        engine.bootstrap(history, TODAY)
    assert error.value.code == "invalid_history"


def test_daily_replay_limit_is_explicit(engine: LocalGameEngine) -> None:
    """По достижении лимита история не удаляется и новая игра не создаётся."""
    history = [_indexed_win(engine, TODAY, index) for index in range(MAX_GAMES_PER_DAY)]
    with pytest.raises(GameError) as error:
        engine.next_game(history, TODAY.isoformat(), MAX_GAMES_PER_DAY - 1, TODAY)
    assert error.value.code == "daily_limit"
    assert len(history) == MAX_GAMES_PER_DAY


def test_personal_words_survive_reload_and_differ_between_players(
    engine: LocalGameEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Персональная выдача стабильна, но не общая для всех аккаунтов."""
    picks = iter(range(20))
    monkeypatch.setattr(
        "prototypes.wordli.engine.secrets.choice",
        lambda available: available[next(picks) % len(available)],
    )
    answers = set()
    for player_id in range(1, 21):
        state = engine.bootstrap([], TODAY, player_id=player_id)
        answers.add(state["history"][-1]["answer"])
        assert engine.bootstrap(state["history"], TODAY, player_id=player_id) == state
        assert state["game"]["answer"] is None
    assert len(answers) > 1


def test_expired_empty_words_are_never_reissued_and_do_not_count_as_losses(
    engine: LocalGameEngine,
) -> None:
    """Даже пустые истёкшие партии исчерпывают личный словарь без поражений."""
    history = []
    answers = []
    for offset in range(len(WORDS)):
        day = TODAY + timedelta(days=offset * 9)
        state = engine.bootstrap(history, day, player_id=77)
        history = state["history"]
        answers.append(history[-1]["answer"])
        assert state["game"]["status"] == "in_progress"
        assert state["stats"]["played"] == 0
    assert len(set(answers)) == len(WORDS)
    exhausted = engine.bootstrap(history, TODAY + timedelta(days=100), player_id=77)
    assert exhausted["game"]["status"] == "exhausted"
    assert exhausted["game"]["answer"] is None
    assert exhausted["availability"] == {"remaining": 0, "exhausted": True}
    assert exhausted["stats"]["wins"] == exhausted["stats"]["played"] == 0
    assert len(exhausted["history"]) == len(WORDS)


def test_extra_games_and_different_days_share_the_same_exclusion_list(
    engine: LocalGameEngine,
) -> None:
    """Победы и поражения исключают слова и из повторов, и из новых дней."""
    state = engine.bootstrap([], TODAY, player_id=123)
    seen = {state["history"][-1]["answer"]}
    for index in range(3):
        answer = state["history"][-1]["answer"]
        if index == 1:
            wrong = next(word for word in WORDS if word != answer)
            for _ in range(6):
                state = engine.submit(
                    state["history"],
                    TODAY.isoformat(),
                    wrong,
                    TODAY,
                    index=index,
                    player_id=123,
                )
        else:
            state = engine.submit(
                state["history"],
                TODAY.isoformat(),
                answer,
                TODAY,
                index=index,
                player_id=123,
            )
        state = engine.next_game(
            state["history"], TODAY.isoformat(), index, TODAY, player_id=123
        )
        answer = state["history"][-1]["answer"]
        assert answer not in seen
        seen.add(answer)
    next_day = TODAY + timedelta(days=30)
    state = engine.bootstrap(state["history"], next_day, player_id=123)
    assert state["history"][-1]["answer"] not in seen
    assert state["stats"]["played"] == 3
    assert state["stats"]["wins"] == 2
    assert state["stats"]["win_streak"] == 1


def test_legacy_duplicates_are_preserved_but_not_selected_again(
    engine: LocalGameEngine,
) -> None:
    """Миграция не переписывает старые дубли и исключает их из будущего."""
    past = TODAY - timedelta(days=len(WORDS))
    history = [_win(engine, past), _win(engine, TODAY)]
    duplicate = engine.word_for_day(TODAY)
    state = engine.bootstrap(history, TODAY, player_id=80)
    assert state["stats"]["wins"] == 2
    assert [item["answer"] for item in state["history"]] == [duplicate] * 2
    state = engine.next_game(
        state["history"], TODAY.isoformat(), 0, TODAY, player_id=80
    )
    assert state["history"][-1]["answer"] != duplicate


@pytest.mark.parametrize(
    "played,wins,average,maximum,pool,expected",
    [
        (0, 0, None, 0, 5000, None),
        (9, 9, 1, 9, 5000, None),
        (10, 0, None, 0, 5000, 0),
        (10, 10, 1, 10, 5000, 404),
        (5000, 5000, 1, 5000, 5000, 10000),
        (5000, 5000, 3, 5000, 5000, 9000),
        (10, 10, 1, 10, 10, 10000),
    ],
)
def test_rating_boundaries(
    played: int,
    wins: int,
    average: float | None,
    maximum: int,
    pool: int,
    expected: int | None,
) -> None:
    """Очки целые, ограничены и недоступны до десяти завершённых игр."""
    assert calculate_score(played, wins, average, maximum, pool) == expected


def test_loss_can_reduce_rating() -> None:
    """Проигрыш снижает очки несмотря на рост числа завершённых игр."""
    before = calculate_score(100, 100, 3, 100, 5000)
    after = calculate_score(101, 100, 3, 100, 5000)
    assert isinstance(before, int) and isinstance(after, int)
    assert after < before


def test_next_game_day_rollover_and_next_year_daily_reset(
    engine: LocalGameEngine,
) -> None:
    """На границе года дневная партия начинается снова с нулевого номера."""
    old = date(2026, 12, 31)
    today = date(2027, 1, 1)
    history = [_indexed_win(engine, old, index) for index in range(2)]
    state = engine.bootstrap(history, today)
    assert state["game"]["day"] == today.isoformat()
    assert state["game"]["index"] == 0
    assert state["stats"]["strict_streak"] == 2
    with pytest.raises(GameError) as error:
        engine.next_game(history, old.isoformat(), 1, today)
    assert error.value.code == "day_changed"
