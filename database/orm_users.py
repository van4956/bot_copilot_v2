"""Регистрация и настройки пользователей без сканирования всей таблицы."""

from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.locales import SUPPORTED_LOCALES, normalize_locale
from database.models import Users


async def orm_get_ids(session: AsyncSession) -> list[int]:
    """Возвращает ID для явных административных выборок."""
    return list(await session.scalars(select(Users.user_id)))


async def orm_add_user(
    session: AsyncSession, data: dict[str, Any]
) -> tuple[Users, bool]:
    """Атомарно регистрирует или обновляет пользователя, сохраняя его язык."""
    values = {
        "user_id": data["user_id"],
        "user_name": str(data.get("user_name") or "")[:150],
        "full_name": str(data.get("full_name") or "Пользователь")[:150],
        "locale": normalize_locale(data.get("locale")),
        "status": data.get("status", "member"),
        "flag": data.get("flag", 1),
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert(Users)
    elif dialect == "sqlite":
        insert = sqlite_insert(Users)
    else:
        raise ValueError("Регистрация поддерживает SQLite и PostgreSQL")
    query = (
        insert.values(**values)
        .on_conflict_do_nothing(index_elements=[Users.user_id])
        .returning(Users.user_id)
    )
    created = await session.scalar(query) is not None
    if not created:
        query = (
            update(Users)
            .where(Users.user_id == values["user_id"])
            .values(
                user_name=values["user_name"],
                full_name=values["full_name"],
                status=values["status"],
                locale=case(
                    (Users.locale.in_(SUPPORTED_LOCALES), Users.locale),
                    else_=values["locale"],
                ),
            )
        )
        await session.execute(query)
    await session.commit()
    user = await orm_get_user(session, values["user_id"])
    assert user is not None
    return user, created


async def orm_get_users(session: AsyncSession) -> list[Users]:
    """Возвращает пользователей для административного списка."""
    query = select(Users).order_by(Users.user_id)
    return list(await session.scalars(query))


async def orm_get_user(
    session: AsyncSession, user_id: int
) -> Users | None:
    """Находит одного пользователя по первичному ключу."""
    query = (
        select(Users)
        .where(Users.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(query)


async def orm_get_locale(
    session: AsyncSession, user_id: int
) -> str | None:
    """Возвращает поддерживаемый язык зарегистрированного пользователя."""
    value = await session.scalar(
        select(Users.locale).where(Users.user_id == user_id)
    )
    if value is None:
        return None
    return normalize_locale(value)


async def orm_update_status(
    session: AsyncSession, user_id: int, data: str
) -> None:
    """Меняет статус без загрузки остальных пользователей."""
    query = update(Users).where(Users.user_id == user_id).values(status=data)
    await session.execute(query)
    await session.commit()


async def orm_update_locale(
    session: AsyncSession, user_id: int, data: str
) -> None:
    """Сохраняет выбранный поддерживаемый язык."""
    if data not in SUPPORTED_LOCALES:
        raise ValueError("Язык не поддерживается")
    query = update(Users).where(Users.user_id == user_id).values(locale=data)
    await session.execute(query)
    await session.commit()
