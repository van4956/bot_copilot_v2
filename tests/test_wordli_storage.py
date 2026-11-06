"""Серверные партии Wordli в отдельных временных SQLite-файлах."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, MetaData, Table, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from prototypes.wordli.engine import GameError, LocalGameEngine
from prototypes.wordli.storage import (
    WordliStorage,
    profiles_table,
    requests_table,
    wordli_metadata,
)

USER = 9000011111
OTHER_USER = 9000022222
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
WORDS = ["БИЛЕТ", "КОМАР", "КОШКА", "ЛАМПА", "ПАРОМ", "ТАЧКА"]


@dataclass
class Clock:
    """Предоставляет управляемые часы только изолированным тестам."""

    value: datetime = NOW

    def now(self) -> datetime:
        """Возвращает установленный момент времени."""
        return self.value


@pytest_asyncio.fixture
async def bundle(
    tmp_path: Path,
) -> AsyncIterator[tuple[WordliStorage, LocalGameEngine, AsyncEngine, Clock]]:
    """Создаёт собственный временный словарь и SQLite, не читая env."""
    dictionary = tmp_path / "words.json"
    dictionary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dictionary_id": "storage-unit-v1",
                "normalization": "Ё=Е",
                "words": WORDS,
                "answers": WORDS,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rules = LocalGameEngine(dictionary)
    database = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'wordli-test.sqlite3'}"
    )
    clock = Clock()
    storage = WordliStorage(database, rules, clock.now)
    try:
        await storage.init_schema()
        yield storage, rules, database, clock
    finally:
        await database.dispose()


async def _win(
    storage: WordliStorage,
    rules: LocalGameEngine,
    state: dict[str, Any],
    user_id: int = USER,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Отправляет настоящий ответ текущей тестовой партии."""
    return await storage.submit(
        user_id=user_id,
        day=state["day"],
        index=state["game"]["index"],
        guess=await _answer(storage, state, user_id),
        revision=state["revision"],
        request_id=request_id or str(uuid4()),
    )


async def _answer(
    storage: WordliStorage,
    state: dict[str, Any],
    user_id: int = USER,
) -> str:
    """Читает ответ только из временной тестовой БД, не через API."""
    async with storage._database.connect() as connection:
        history = await connection.scalar(
            select(profiles_table.c.history).where(profiles_table.c.user_id == user_id)
        )
    return next(
        record["answer"]
        for record in history
        if record["day"] == state["day"] and record["index"] == state["game"]["index"]
    )


async def _ledger_size(database: AsyncEngine) -> int:
    """Читает число записей только выделенного тестового журнала."""
    async with database.connect() as connection:
        return await connection.scalar(select(func.count()).select_from(requests_table))


async def test_schema_is_isolated_from_unrelated_tables(bundle) -> None:
    """Инициализация не использует общую метадату и чужие таблицы."""
    storage, _, database, _ = bundle
    metadata = MetaData()
    unrelated = Table(
        "unrelated_data",
        metadata,
        Column("value", Integer, primary_key=True),
    )
    async with database.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(unrelated.insert().values(value=77))
    await storage.init_schema()
    assert set(wordli_metadata.tables) == {
        "wordli_profiles",
        "wordli_requests",
    }
    async with database.connect() as connection:
        assert await connection.scalar(select(unrelated.c.value)) == 77


async def test_empty_state_is_private_and_server_authoritative(bundle) -> None:
    """Новый профиль не получает фиктивных побед и полного журнала ходов."""
    storage, _, _, _ = bundle
    state = await storage.bootstrap(USER)
    assert state["game"]["rows"] == []
    assert state["game"]["answer"] is None
    assert state["stats"]["played"] == state["stats"]["wins"] == 0
    assert state["revision"] >= 0
    assert "history" not in state
    assert await storage.bootstrap(USER) == state
    with pytest.raises(TypeError):
        await storage.bootstrap(USER, history=[{"outcome": "win"}])


async def test_identity_isolation_and_persistence_across_instances(
    bundle,
) -> None:
    """Один пользователь видит свою победу, другой — новую пустую партию."""
    storage, rules, database, clock = bundle
    first = await storage.bootstrap(USER)
    won = await _win(storage, rules, first)
    reloaded = WordliStorage(database, rules, clock.now)
    assert await reloaded.bootstrap(USER) == won
    other = await reloaded.bootstrap(OTHER_USER)
    assert other["game"]["guesses"] == []
    assert other["game"]["answer"] is None
    assert other["stats"]["wins"] == 0


async def test_database_restart_keeps_server_history(bundle) -> None:
    """Повторное открытие временного файла восстанавливает принятое слово."""
    storage, rules, database, clock = bundle
    state = await storage.bootstrap(USER)
    won = await _win(storage, rules, state)
    url = database.url
    await database.dispose()
    reopened = create_async_engine(url)
    try:
        service = WordliStorage(reopened, rules, clock.now)
        assert await service.bootstrap(USER) == won
    finally:
        await reopened.dispose()


async def test_exact_request_retry_returns_stored_response(bundle) -> None:
    """Повтор UUID сохраняет один ход даже с устаревшей версией запроса."""
    storage, rules, database, _ = bundle
    initial = await storage.bootstrap(USER)
    request_id = str(uuid4())
    won = await _win(storage, rules, initial, request_id=request_id)
    retried = await _win(storage, rules, initial, request_id=request_id)
    assert retried == won
    assert won["stats"]["played"] == won["stats"]["wins"] == 1
    assert await _ledger_size(database) == 1
    current = await storage.next_game(
        USER, won["day"], 0, won["revision"], str(uuid4())
    )
    assert current["game"]["index"] == 1
    assert await _win(storage, rules, initial, request_id=request_id) == won
    assert (await storage.bootstrap(USER))["game"]["index"] == 1


async def test_concurrent_identical_requests_are_idempotent(bundle) -> None:
    """Одновременные одинаковые запросы создают единственную победу."""
    storage, rules, database, clock = bundle
    state = await storage.bootstrap(USER)
    identifier = str(uuid4())
    other_instance = WordliStorage(database, rules, clock.now)
    responses = await asyncio.gather(
        *(
            _win(service, rules, state, request_id=identifier)
            for service in [storage, other_instance] * 4
        )
    )
    assert all(response == responses[0] for response in responses)
    assert await _ledger_size(database) == 1
    assert len(responses[0]["game"]["rows"]) == 1


async def test_concurrent_first_load_and_separate_engines(bundle) -> None:
    """Разные соединения одного файла разделяют блокировку нового профиля."""
    storage, rules, database, clock = bundle
    separate = create_async_engine(database.url)
    try:
        other = WordliStorage(separate, rules, clock.now)
        initial, duplicate = await asyncio.gather(
            storage.bootstrap(USER), other.bootstrap(USER)
        )
        assert initial == duplicate
        identifier = str(uuid4())
        won, repeated = await asyncio.gather(
            _win(storage, rules, initial, request_id=identifier),
            _win(other, rules, initial, request_id=identifier),
        )
        assert won == repeated
        assert await _ledger_size(database) == 1
    finally:
        await separate.dispose()


async def test_next_request_is_idempotent_and_old_game_is_rejected(
    bundle,
) -> None:
    """Повтор перехода не пропускает слово, старое поле нельзя изменять."""
    storage, rules, _, _ = bundle
    won = await _win(storage, rules, await storage.bootstrap(USER))
    identifier = str(uuid4())
    current = await storage.next_game(USER, won["day"], 0, won["revision"], identifier)
    assert (
        await storage.next_game(USER, won["day"], 0, won["revision"], identifier)
        == current
    )
    with pytest.raises(GameError) as error:
        await storage.set_draft(USER, current["day"], 0, current["revision"], "КО")
    assert error.value.code == "game_changed"
    with pytest.raises(GameError) as error:
        await storage.submit(
            USER,
            current["day"],
            0,
            WORDS[0],
            current["revision"],
            str(uuid4()),
        )
    assert error.value.code == "game_changed"
    assert await storage.bootstrap(USER) == current


async def test_request_identifiers_are_isolated_by_user(bundle) -> None:
    """Один UUID разных пользователей не смешивает их партии."""
    storage, rules, database, _ = bundle
    identifier = str(uuid4())
    for user_id in (USER, OTHER_USER):
        initial = await storage.bootstrap(user_id)
        won = await _win(
            storage, rules, initial, user_id=user_id, request_id=identifier
        )
        assert won["stats"]["wins"] == 1
    assert await _ledger_size(database) == 2


async def test_reused_request_id_with_new_payload_is_rejected(bundle) -> None:
    """UUID нельзя использовать для другого слова или другого действия."""
    storage, rules, _, _ = bundle
    state = await storage.bootstrap(USER)
    identifier = str(uuid4())
    won = await _win(storage, rules, state, request_id=identifier)
    expected = await _answer(storage, state)
    wrong = next(word for word in WORDS if word != expected)
    with pytest.raises(GameError) as error:
        await storage.submit(
            USER, state["day"], 0, wrong, state["revision"], identifier
        )
    assert error.value.code == "request_id_conflict"
    assert error.value.status == 409
    with pytest.raises(GameError) as error:
        await storage.next_game(USER, won["day"], 0, won["revision"], identifier)
    assert error.value.code == "request_id_conflict"
    assert await storage.bootstrap(USER) == won


async def test_concurrent_different_requests_cannot_overwrite_a_move(
    bundle,
) -> None:
    """Из двух новых ходов одной версии принимается только один."""
    storage, rules, database, _ = bundle
    initial = await storage.bootstrap(USER)
    expected = await _answer(storage, initial)
    wrong = [word for word in WORDS if word != expected][:2]
    results = await asyncio.gather(
        *(
            storage.submit(
                USER,
                initial["day"],
                0,
                word,
                initial["revision"],
                str(uuid4()),
            )
            for word in wrong
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, dict) for value in results) == 1
    failure = next(value for value in results if isinstance(value, GameError))
    assert failure.code == "version_conflict"
    assert failure.status == 409
    assert len((await storage.bootstrap(USER))["game"]["rows"]) == 1
    assert await _ledger_size(database) == 1


async def test_draft_is_persistent_without_spending_an_attempt(bundle) -> None:
    """Черновик сохраняется с новой версией без принятого хода."""
    storage, rules, database, clock = bundle
    initial = await storage.bootstrap(USER)
    saved = await storage.set_draft(
        USER, initial["day"], 0, initial["revision"], "бе\u0308д"
    )
    assert saved["game"]["draft"] == "БЕД"
    assert saved["game"]["rows"] == []
    assert saved["revision"] == initial["revision"] + 1
    duplicate = await storage.set_draft(USER, saved["day"], 0, saved["revision"], "БЕД")
    assert duplicate == saved
    reloaded = WordliStorage(database, rules, clock.now)
    assert await reloaded.bootstrap(USER) == saved
    assert await _ledger_size(database) == 0
    with pytest.raises(GameError) as error:
        await storage.set_draft(USER, initial["day"], 0, initial["revision"], "КО")
    assert error.value.code == "version_conflict"


async def test_rejected_word_does_not_change_revision_or_ledger(
    bundle,
) -> None:
    """Неизвестное слово не записывается как ход или выполненный запрос."""
    storage, rules, database, _ = bundle
    state = await storage.bootstrap(USER)
    identifier = str(uuid4())
    with pytest.raises(GameError) as error:
        await storage.submit(
            USER, state["day"], 0, "АБВГД", state["revision"], identifier
        )
    assert error.value.code == "unknown_word"
    assert await storage.bootstrap(USER) == state
    assert await _ledger_size(database) == 0
    result = await _win(storage, rules, state, request_id=identifier)
    assert result["stats"]["wins"] == 1


async def test_extra_games_and_losses_use_real_server_statistics(
    bundle,
) -> None:
    """Сервер пересчитывает серию и гистограмму через дополнительные партии."""
    storage, rules, _, _ = bundle
    state = await _win(storage, rules, await storage.bootstrap(USER))
    state = await storage.next_game(
        USER, state["day"], 0, state["revision"], str(uuid4())
    )
    assert state["game"]["answer"] is None
    expected = await _answer(storage, state)
    wrong = next(word for word in WORDS if word != expected)
    for _ in range(6):
        state = await storage.submit(
            USER, state["day"], 1, wrong, state["revision"], str(uuid4())
        )
    assert state["game"]["outcome"] == "loss"
    assert state["stats"]["played"] == 2
    assert state["stats"]["wins"] == 1
    assert state["stats"]["win_streak"] == 0
    assert state["stats"]["strict_streak"] == 0
    state = await storage.next_game(
        USER, state["day"], 1, state["revision"], str(uuid4())
    )
    state = await _win(storage, rules, state)
    assert state["stats"]["wins"] == 2
    assert state["stats"]["played"] == 3
    assert state["stats"]["distribution"] == [2, 0, 0, 0, 0, 0]
    assert state["stats"]["win_streak"] == 1
    assert state["progression"]["total_wins"] == 2


async def test_expiry_keeps_progress_without_counting_a_loss(bundle) -> None:
    """Новый серверный день не превращает незавершённую игру в поражение."""
    storage, rules, _, clock = bundle
    state = await storage.bootstrap(USER)
    expected = await _answer(storage, state)
    wrong = next(word for word in WORDS if word != expected)
    state = await storage.submit(
        USER, state["day"], 0, wrong, state["revision"], str(uuid4())
    )
    clock.value += timedelta(days=1)
    current = await storage.bootstrap(USER)
    assert current["day"] == "2026-11-06"
    assert current["game"]["rows"] == []
    assert current["game"]["answer"] is None
    assert current["stats"]["played"] == 0
    assert current["revision"] == state["revision"] + 1


async def test_stale_day_requests_do_not_mutate_history(bundle) -> None:
    """Запоздалый черновик или переход не меняет истёкшую партию."""
    storage, rules, database, clock = bundle
    won = await _win(storage, rules, await storage.bootstrap(USER))
    clock.value += timedelta(days=1)
    with pytest.raises(GameError) as error:
        await storage.set_draft(USER, won["day"], 0, won["revision"], "КО")
    assert error.value.code == "day_changed"
    with pytest.raises(GameError) as error:
        await storage.next_game(USER, won["day"], 0, won["revision"], str(uuid4()))
    assert error.value.code == "day_changed"
    current = await storage.bootstrap(USER)
    assert current["stats"]["wins"] == 1
    assert current["stats"]["strict_streak"] == 1
    assert current["game"]["rows"] == []
    assert await _ledger_size(database) == 1


async def test_server_day_uses_utc_not_clock_local_offset(bundle) -> None:
    """Полночь наступает по UTC, а не по часовому поясу устройства."""
    storage, _, _, clock = bundle
    clock.value = datetime(2026, 11, 6, 2, 0, tzinfo=timezone(timedelta(hours=4)))
    assert (await storage.bootstrap(USER))["day"] == "2026-11-05"
    clock.value += timedelta(hours=2)
    assert (await storage.bootstrap(USER))["day"] == "2026-11-06"


async def test_dictionary_change_does_not_reinterpret_saved_games(
    bundle, tmp_path: Path
) -> None:
    """Смена версии словаря блокируется, исходная серверная история цела."""
    storage, rules, database, clock = bundle
    won = await _win(storage, rules, await storage.bootstrap(USER))
    data = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    data["dictionary_id"] = "storage-unit-v2"
    filename = tmp_path / "changed.json"
    filename.write_text(json.dumps(data), encoding="utf-8")
    changed = WordliStorage(database, LocalGameEngine(filename), clock.now)
    with pytest.raises(GameError) as error:
        await changed.bootstrap(USER)
    assert error.value.code == "dictionary_changed"
    assert error.value.status == 409
    assert await storage.bootstrap(USER) == won


async def test_transaction_failure_rolls_back_history_and_request(
    bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ошибка после UPDATE не оставляет частично принятую победу."""
    storage, rules, database, _ = bundle
    initial = await storage.bootstrap(USER)
    original = storage._save

    async def fail_after_update(session, profile, history) -> int:
        """Проверяет откат реального SQL-изменения при сбое операции."""
        await original(session, profile, history)
        raise RuntimeError("Simulated transaction failure")

    monkeypatch.setattr(storage, "_save", fail_after_update)
    with pytest.raises(RuntimeError):
        await _win(storage, rules, initial)
    monkeypatch.setattr(storage, "_save", original)
    assert await storage.bootstrap(USER) == initial
    assert await _ledger_size(database) == 0


@pytest.mark.parametrize("user_id", [0, -1, True, "123", None, 2**63])
async def test_invalid_identity_never_creates_profile(bundle, user_id) -> None:
    """Непроверенные идентификаторы отклоняются до записи профиля."""
    storage, _, database, _ = bundle
    with pytest.raises(GameError) as error:
        await storage.bootstrap(user_id)
    assert error.value.code == "invalid_user"
    async with database.connect() as connection:
        assert (
            await connection.scalar(select(func.count()).select_from(profiles_table))
            == 0
        )


@pytest.mark.parametrize("identifier", [None, "", "not-a-uuid", 123])
async def test_invalid_request_id_is_rejected(bundle, identifier) -> None:
    """Ход без допустимого UUID не выполняется."""
    storage, rules, _, _ = bundle
    state = await storage.bootstrap(USER)
    with pytest.raises(GameError) as error:
        await storage.submit(
            USER,
            state["day"],
            0,
            rules.word_for_day(NOW.date()),
            state["revision"],
            identifier,
        )
    assert error.value.code == "invalid_request_id"
    assert await storage.bootstrap(USER) == state


async def test_personal_assignments_persist_and_reserve_every_expired_word(
    bundle,
) -> None:
    """Список выданных слов сохраняется в БД независимо от исходов и перезапуска."""
    storage, rules, database, clock = bundle
    seen = set()
    for offset in range(len(WORDS)):
        state = await storage.bootstrap(USER)
        answer = await _answer(storage, state)
        assert answer not in seen
        seen.add(answer)
        storage = WordliStorage(database, rules, clock.now)
        assert await storage.bootstrap(USER) == state
        clock.value += timedelta(days=7)
    exhausted = await storage.bootstrap(USER)
    assert exhausted["game"]["status"] == "exhausted"
    assert exhausted["game"]["answer"] is None
    assert exhausted["availability"] == {"remaining": 0, "exhausted": True}
    assert exhausted["stats"]["played"] == 0
    assert "history" not in exhausted
    async with database.connect() as connection:
        history = await connection.scalar(
            select(profiles_table.c.history).where(profiles_table.c.user_id == USER)
        )
    assert {record["answer"] for record in history} == set(WORDS)


async def test_finished_last_word_keeps_statistics_and_disallows_replay(bundle) -> None:
    """Последняя победа остаётся доступной без повтора исчерпанного словаря."""
    storage, rules, _, clock = bundle
    state = await storage.bootstrap(USER)
    for index in range(len(WORDS)):
        state = await _win(storage, rules, state)
        if index + 1 < len(WORDS):
            state = await storage.next_game(
                USER, state["day"], index, state["revision"], str(uuid4())
            )
    assert state["stats"]["wins"] == len(WORDS)
    with pytest.raises(GameError) as error:
        await storage.next_game(
            USER, state["day"], len(WORDS) - 1, state["revision"], str(uuid4())
        )
    assert error.value.code == "no_more_words"
    assert await storage.bootstrap(USER) == state
    clock.value += timedelta(days=1)
    exhausted = await storage.bootstrap(USER)
    assert exhausted["stats"] == state["stats"]
    assert exhausted["game"]["status"] == "exhausted"


async def test_known_archive_migrates_old_history_without_changing_active_answer(
    bundle,
    tmp_path: Path,
) -> None:
    """Новый словарь закрепляет старые ответы и сохраняет черновик и победы."""
    storage, rules, database, clock = bundle
    state = await storage.bootstrap(USER)
    answer = await _answer(storage, state)
    state = await storage.set_draft(USER, state["day"], 0, state["revision"], "КО")
    original_path = tmp_path / "words.json"
    old_payload = json.loads(original_path.read_text(encoding="utf-8"))
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / f"{rules.dictionary_id}.json").write_text(
        json.dumps(old_payload, ensure_ascii=False), encoding="utf-8"
    )
    # Восстанавливаем действительно старую схему без закреплённых ответов.
    old_answer = rules.word_for_day(NOW.date())
    legacy_history = [
        {
            "day": (NOW.date() - timedelta(days=2)).isoformat(),
            "index": 0,
            "guesses": [rules.word_for_day(NOW.date() - timedelta(days=2))],
            "draft": "",
        },
        {"day": state["day"], "index": 0, "guesses": [], "draft": "КО"},
    ]
    async with database.begin() as connection:
        await connection.execute(
            profiles_table.update()
            .where(profiles_table.c.user_id == USER)
            .values(history=legacy_history)
        )
    new_payload = {
        **old_payload,
        "dictionary_id": "storage-unit-v2",
        "words": [*WORDS, "БУКВА"],
        "answers": [*WORDS, "БУКВА"],
    }
    new_path = tmp_path / "new.json"
    new_path.write_text(json.dumps(new_payload, ensure_ascii=False), encoding="utf-8")
    new_rules = LocalGameEngine(new_path)
    updated = WordliStorage(database, new_rules, clock.now)
    migrated = await updated.bootstrap(USER)
    assert migrated["game"]["draft"] == "КО"
    assert migrated["game"]["answer"] is None
    assert await _answer(updated, migrated) == old_answer
    assert migrated["revision"] > state["revision"]
    assert migrated["dictionary"]["id"] == "storage-unit-v2"
    assert migrated["stats"]["played"] == migrated["stats"]["wins"] == 1
    assert migrated["stats"]["win_streak"] == 1
    assert await updated.bootstrap(USER) == migrated
    won = await _win(updated, new_rules, migrated)
    assert won["game"]["answer"] == old_answer
    assert won["stats"]["wins"] == 2
    following = await updated.next_game(
        USER, won["day"], 0, won["revision"], str(uuid4())
    )
    assert await _answer(updated, following) != old_answer
    assert answer in WORDS


async def test_dictionary_upgrade_keeps_pinned_answer_even_if_removed_from_new_pool(
    bundle,
    tmp_path: Path,
) -> None:
    """Сохранённую партию можно закончить после изменения списка ответов."""
    storage, rules, database, clock = bundle
    state = await storage.bootstrap(USER)
    answer = await _answer(storage, state)
    payload = json.loads((tmp_path / "words.json").read_text(encoding="utf-8"))
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / f"{rules.dictionary_id}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    payload["dictionary_id"] = "storage-unit-v2"
    payload["words"] = payload["answers"] = [word for word in WORDS if word != answer]
    filename = tmp_path / "new.json"
    filename.write_text(json.dumps(payload), encoding="utf-8")
    new_rules = LocalGameEngine(filename)
    updated = WordliStorage(database, new_rules, clock.now)
    migrated = await updated.bootstrap(USER)
    won = await _win(updated, new_rules, migrated)
    assert won["game"]["answer"] == answer
    assert won["stats"]["wins"] == 1
