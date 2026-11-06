"""Проверки зафиксированного игрового словаря без загрузки библиотек."""

import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path

from prototypes.wordli.engine import LocalGameEngine

DATA = Path(__file__).resolve().parents[1] / "prototypes" / "wordli" / "data"


def test_dictionary_shape_and_integrity() -> None:
    """Проверяет размер, нормализацию, уникальность и контрольные суммы."""
    payload = json.loads((DATA / "ru5.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["normalization"] == "Ё=Е"
    assert len(payload["words"]) == 2083
    assert len(payload["answers"]) == 1758
    for name in ["words", "answers"]:
        words = payload[name]
        assert words == sorted(set(words))
        assert all(re.fullmatch("[А-Я]{5}", word) for word in words)
        digest = hashlib.sha256(("\n".join(words) + "\n").encode()).hexdigest()
        assert digest == payload[f"{name}_sha256"]
    assert set(payload["answers"]).issubset(payload["words"])
    assert {"КОШКА", "ТАЧКА", "КОМАР", "КНИГА", "СЛОВО"}.issubset(
        payload["words"]
    )


def test_declared_exclusions_are_not_reintroduced() -> None:
    """Явные исключения и ограничения ответов сохраняются при пересборке."""
    payload = json.loads((DATA / "ru5.json").read_text(encoding="utf-8"))
    rules = json.loads(
        (DATA / "selection_rules.json").read_text(encoding="utf-8")
    )
    excluded = {word for group in rules["excluded"].values() for word in group}
    assert not excluded.intersection(payload["words"])
    assert not set(rules["answer_excluded"]).intersection(payload["answers"])


def test_dictionary_provenance_and_runtime_independence() -> None:
    """Проверяет происхождение и независимость от инструментов отбора."""
    payload = json.loads((DATA / "ru5.json").read_text(encoding="utf-8"))
    names = {item["name"] for item in payload["sources"]}
    assert {"pymorphy3-dicts-ru", "pymorphy3", "wordfreq"}.issubset(names)
    assert all(
        re.fullmatch("[0-9a-f]{64}", item["sha256"])
        for item in payload["sources"]
    )
    assert payload["license"] == "CC BY-SA 4.0"
    assert (DATA / "SOURCES.md").is_file()
    assert (DATA / "LICENSE.md").is_file()
    engine = LocalGameEngine()
    assert engine.dictionary_size == 2083
    assert engine.bootstrap([], date(2026, 11, 5))["game"]["answer"] is None


def test_real_daily_cycle_has_no_repeated_answers() -> None:
    """Один полный цикл реального набора не повторяет ежедневные ответы."""
    engine = LocalGameEngine()
    beginning = date(2026, 1, 1)
    payload = json.loads((DATA / "ru5.json").read_text(encoding="utf-8"))
    answer_count = len(payload["answers"])
    words = [
        engine.word_for_day(beginning + timedelta(days=index))
        for index in range(answer_count)
    ]
    assert len(set(words)) == answer_count


def test_expansion_keeps_old_words_and_archives_original_answers() -> None:
    """Сохраняет исходный словарь для восстановления старых партий."""
    identifier = "ru5-opencorpora-wordfreq-v1-165050f1e2b8"
    original = json.loads(
        (DATA / "archive" / f"{identifier}.json").read_text(encoding="utf-8")
    )
    current = json.loads((DATA / "ru5.json").read_text(encoding="utf-8"))
    assert original["dictionary_id"] == identifier
    assert len(original["words"]) == 2000
    assert len(original["answers"]) == 1200
    assert set(original["words"]).issubset(current["words"])
    assert set(original["answers"]).issubset(current["answers"])
    for name in ("words", "answers"):
        digest = hashlib.sha256(
            ("\n".join(original[name]) + "\n").encode()
        ).hexdigest()
        assert digest == original[f"{name}_sha256"]


def test_expansion_uses_reviewed_words_without_lowering_frequency_limits() -> None:
    """Добавляет просмотренные слова, не ослабляя автоматический отбор."""
    current = json.loads((DATA / "ru5.json").read_text(encoding="utf-8"))
    rules = json.loads(
        (DATA / "selection_rules.json").read_text(encoding="utf-8")
    )
    original = json.loads(
        (
            DATA / "archive" / "ru5-opencorpora-wordfreq-v1-165050f1e2b8.json"
        ).read_text(encoding="utf-8")
    )
    assert rules["minimum_zipf"] >= 2.3
    assert rules["answer_minimum_zipf"] >= 3.0
    assert rules["target_words"] is None
    additions = set(current["answers"]) - set(original["answers"])
    assert len(additions) == 558
    assert additions.issubset(rules["common_candidates"])
    assert {"БУЛКА", "КОФТА", "НОРКА", "ШМЕЛЬ", "БИДОН", "КОПНА"}.issubset(
        additions
    )
    new_guesses = set(current["words"]) - set(original["words"])
    assert new_guesses.issubset(rules["common_candidates"])
    canonical_rules = json.dumps(
        rules, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert hashlib.sha256(canonical_rules).hexdigest() == (
        current["selection"]["rules_sha256"]
    )
