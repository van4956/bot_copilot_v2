"""Серверное сохранение Wordli в отдельных таблицах без моделей бота."""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from weakref import WeakKeyDictionary, WeakValueDictionary

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    ForeignKey,
    MetaData,
    String,
    Table,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from .engine import (
    DRAFT_PATTERN,
    MAX_GAMES_PER_DAY,
    GameError,
    LocalGameEngine,
)

wordli_metadata = MetaData()
profiles_table = Table(
    "wordli_profiles",
    wordli_metadata,
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("history", JSON, nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("dictionary_id", String(128), nullable=False),
)
requests_table = Table(
    "wordli_requests",
    wordli_metadata,
    Column(
        "user_id",
        BigInteger,
        ForeignKey("wordli_profiles.user_id"),
        primary_key=True,
        autoincrement=False,
    ),
    Column("request_id", String(36), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("response", JSON, nullable=False),
)

# SQLite не имеет SELECT FOR UPDATE; блокировки общие для экземпляров в loop.
_sqlite_locks: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    WeakValueDictionary[tuple[str, int], asyncio.Lock],
] = WeakKeyDictionary()


def _now() -> datetime:
    """Читает серверные часы в UTC."""
    return datetime.now(timezone.utc)


def _check_user(user_id: int) -> None:
    """Принимает только положительный Telegram ID пользователя."""
    if type(user_id) is not int or not 0 < user_id <= 2**63 - 1:
        raise GameError("invalid_user", "Некорректный пользователь.")


def _check_revision(revision: int) -> None:
    """Ограничивает ожидаемую версию состояния целым счётчиком."""
    if type(revision) is not int or not 0 <= revision <= 2**63 - 1:
        raise GameError("invalid_revision", "Некорректная версия партии.")


def _check_position(day: str, index: int) -> None:
    """Проверяет координаты партии до обращения к хранилищу."""
    if not isinstance(day, str):
        raise GameError("invalid_day", "Некорректный игровой день.")
    try:
        valid_day = date.fromisoformat(day)
    except ValueError:
        raise GameError("invalid_day", "Некорректный игровой день.") from None
    if valid_day.isoformat() != day:
        raise GameError("invalid_day", "Некорректный игровой день.")
    if type(index) is not int or not 0 <= index < MAX_GAMES_PER_DAY:
        raise GameError("invalid_index", "Некорректный номер партии.")


def _request_uuid(value: str) -> str:
    """Канонизирует обязательный UUID идемпотентной операции."""
    if not isinstance(value, str):
        raise GameError("invalid_request_id", "Некорректный ID запроса.")
    try:
        return str(UUID(value))
    except ValueError:
        raise GameError("invalid_request_id", "Некорректный ID запроса.") from None


def _version_conflict() -> GameError:
    """Сообщает о конфликте без потери серверного прогресса."""
    return GameError(
        "version_conflict",
        "Партия изменилась. Загрузите актуальное поле.",
        409,
    )


def _public_state(state: dict[str, Any], revision: int) -> dict[str, Any]:
    """Не отдаёт клиенту историю, из которой рассчитываются результаты."""
    return {key: value for key, value in state.items() if key != "history"} | {
        "revision": revision
    }


class WordliStorage:
    """Хранит только серверные партии и личную статистику Wordli."""

    def __init__(
        self,
        db_engine: AsyncEngine,
        game_engine: LocalGameEngine,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Принимает проверенный снаружи движок БД; не открывает соединения."""
        if db_engine.dialect.name not in {"postgresql", "sqlite"}:
            raise ValueError("Поддерживаются только PostgreSQL и SQLite")
        self._database = db_engine
        self._rules = game_engine
        self._clock = clock if clock is not None else _now
        self._sessions = async_sessionmaker(db_engine, expire_on_commit=False)
        self._dialect = db_engine.dialect.name
        database = db_engine.url.database
        self._sqlite_identity = (
            str(Path(database).resolve())
            if database and database != ":memory:"
            else f"memory:{id(db_engine)}"
        )

    async def init_schema(self) -> None:
        """Создаёт только две таблицы из самостоятельной метадаты Wordli."""
        async with self._database.begin() as connection:
            await connection.run_sync(wordli_metadata.create_all)

    def _today(self) -> date:
        """Использует только осведомлённые о часовом поясе серверные часы."""
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("Серверные часы должны включать часовой пояс")
        return value.astimezone(timezone.utc).date()

    @asynccontextmanager
    async def _serialized(self, user_id: int) -> AsyncIterator[None]:
        """Сериализует SQLite-запросы пользователя внутри одного процесса."""
        if self._dialect != "sqlite":
            yield
            return
        loop = asyncio.get_running_loop()
        registry = _sqlite_locks.setdefault(loop, WeakValueDictionary())
        key = (self._sqlite_identity, user_id)
        lock = registry.get(key)
        if lock is None:
            lock = asyncio.Lock()
            registry[key] = lock
        async with lock:
            yield

    async def _profile(self, session: AsyncSession, user_id: int) -> RowMapping:
        """Блокирует профиль PostgreSQL и безопасно создаёт новый при гонке."""
        query = (
            select(profiles_table)
            .where(profiles_table.c.user_id == user_id)
            .with_for_update()
        )
        profile = (await session.execute(query)).mappings().one_or_none()
        if profile is None:
            insert = pg_insert if self._dialect == "postgresql" else sqlite_insert
            create = insert(profiles_table).values(
                user_id=user_id,
                history=[],
                revision=0,
                dictionary_id=self._rules.dictionary_id,
            )
            await session.execute(
                create.on_conflict_do_nothing(index_elements=[profiles_table.c.user_id])
            )
            profile = (await session.execute(query)).mappings().one()
        if profile["dictionary_id"] != self._rules.dictionary_id:
            history = self._rules.migrate_history(
                profile["history"], profile["dictionary_id"], self._today()
            )
            changed = await session.execute(
                update(profiles_table)
                .where(
                    profiles_table.c.user_id == user_id,
                    profiles_table.c.revision == profile["revision"],
                )
                .values(
                    history=history,
                    dictionary_id=self._rules.dictionary_id,
                    revision=profile["revision"] + 1,
                )
            )
            if changed.rowcount != 1:
                raise _version_conflict()
            profile = (await session.execute(query)).mappings().one()
        return profile

    async def _save(
        self,
        session: AsyncSession,
        profile: RowMapping,
        history: list,
    ) -> int:
        """Обновляет историю вместе с версией либо отклоняет конфликт."""
        if history == profile["history"]:
            return profile["revision"]
        revision = profile["revision"] + 1
        result = await session.execute(
            update(profiles_table)
            .where(
                profiles_table.c.user_id == profile["user_id"],
                profiles_table.c.revision == profile["revision"],
            )
            .values(history=history, revision=revision)
        )
        if result.rowcount != 1:
            raise _version_conflict()
        return revision

    async def bootstrap(self, user_id: int) -> dict[str, Any]:
        """Восстанавливает только серверную историю указанного пользователя."""
        _check_user(user_id)
        async with self._serialized(user_id):
            async with self._sessions.begin() as session:
                profile = await self._profile(session, user_id)
                state = self._rules.bootstrap(
                    profile["history"], today=self._today(), player_id=user_id
                )
                revision = await self._save(session, profile, state["history"])
                return _public_state(state, revision)

    async def submit(
        self,
        user_id: int,
        day: str,
        index: int,
        guess: str,
        revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        """Записывает допустимый ход атомарно с результатом его UUID."""
        if not isinstance(guess, str) or len(guess) > 64:
            raise GameError("invalid_word", "Введите слово из пяти букв.")
        return await self._change(
            user_id,
            "guess",
            {"day": day, "index": index, "guess": guess},
            revision,
            request_id,
        )

    async def next_game(
        self,
        user_id: int,
        day: str,
        index: int,
        revision: int,
        request_id: str,
    ) -> dict[str, Any]:
        """Создаёт следующую серверную партию после завершённой."""
        return await self._change(
            user_id,
            "next",
            {"day": day, "index": index},
            revision,
            request_id,
        )

    async def set_draft(
        self,
        user_id: int,
        day: str,
        index: int,
        revision: int,
        draft: str,
    ) -> dict[str, Any]:
        """Сохраняет незавершённый ввод без расходования попытки."""
        if not isinstance(draft, str) or len(draft) > 64:
            raise GameError("invalid_draft", "Некорректная строка ввода.")
        draft = unicodedata.normalize("NFC", draft).strip().upper().replace("Ё", "Е")
        if not DRAFT_PATTERN.fullmatch(draft):
            raise GameError("invalid_draft", "Некорректная строка ввода.")
        return await self._change(
            user_id,
            "draft",
            {"day": day, "index": index, "draft": draft},
            revision,
        )

    async def _change(
        self,
        user_id: int,
        action: str,
        details: dict[str, Any],
        revision: int,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Сериализует проверку версии, ход и запись идемпотентного ответа."""
        _check_user(user_id)
        _check_revision(revision)
        _check_position(details["day"], details["index"])
        identifier = _request_uuid(request_id) if request_id is not None else None
        if action != "draft" and identifier is None:
            raise GameError("invalid_request_id", "Не задан ID запроса.")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"action": action, "revision": revision, **details},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        async with self._serialized(user_id):
            async with self._sessions.begin() as session:
                profile = await self._profile(session, user_id)
                if identifier is not None:
                    query = select(requests_table).where(
                        requests_table.c.user_id == user_id,
                        requests_table.c.request_id == identifier,
                    )
                    previous = (await session.execute(query)).mappings().one_or_none()
                    if previous is not None:
                        if previous["fingerprint"] != fingerprint:
                            raise GameError(
                                "request_id_conflict",
                                "ID запроса уже использован для другого хода.",
                                409,
                            )
                        return previous["response"]
                if profile["revision"] != revision:
                    raise _version_conflict()
                current = self._today()
                if action == "guess":
                    state = self._rules.submit(
                        profile["history"],
                        details["day"],
                        details["guess"],
                        today=current,
                        index=details["index"],
                        player_id=user_id,
                    )
                elif action == "next":
                    state = self._rules.next_game(
                        profile["history"],
                        details["day"],
                        details["index"],
                        today=current,
                        player_id=user_id,
                    )
                else:
                    state = self._draft_state(
                        profile["history"], details, current, user_id
                    )
                new_revision = await self._save(session, profile, state["history"])
                response = _public_state(state, new_revision)
                if identifier is not None:
                    await session.execute(
                        requests_table.insert().values(
                            user_id=user_id,
                            request_id=identifier,
                            fingerprint=fingerprint,
                            response=response,
                        )
                    )
                return response

    def _draft_state(
        self,
        history: list,
        details: dict[str, Any],
        today: date,
        player_id: int = 0,
    ) -> dict[str, Any]:
        """Меняет только черновик активной партии, не принимая ходы клиента."""
        state = self._rules.bootstrap(history, today=today, player_id=player_id)
        if state["game"]["status"] == "exhausted":
            raise GameError("no_more_words", "Все доступные слова уже сыграны.", 409)
        if details["day"] != state["day"]:
            raise GameError(
                "day_changed",
                "Игровой день сменился. Открыто новое слово.",
                409,
            )
        if details["index"] != state["game"]["index"]:
            raise GameError(
                "game_changed",
                "Партия изменилась. Загрузите актуальное поле.",
                409,
            )
        if state["game"]["outcome"] is not None and details["draft"]:
            raise GameError("game_finished", "Эта партия уже завершена.", 409)
        for record in state["history"]:
            if record["day"] == details["day"] and record["index"] == details["index"]:
                record["draft"] = details["draft"]
                break
        return self._rules.bootstrap(state["history"], today=today, player_id=player_id)
