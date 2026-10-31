"""Сохранение игровых результатов и получение рейтинга."""

from sqlalchemy import desc, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Games


async def orm_add_game(session: AsyncSession, data: dict) -> None:
    """Сохраняет результат и откатывает неудачную транзакцию."""
    obj = Games(
        user_id=data["user_id"],
        user_name=data["user_name"],
        game_name=data["game_name"],
        score=data["score"],
    )
    session.add(obj)
    try:
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        raise


async def orm_get_games(session: AsyncSession) -> list[Games]:
    """Возвращает сохранённые результаты игр."""
    result = await session.execute(select(Games))
    return list(result.scalars().all())


async def get_top_scores(
    session: AsyncSession, game_name: str, limit: int = 10
) -> list[Games]:
    """Возвращает лучшие игровые результаты выбранной игры."""
    query = (
        select(Games)
        .where(Games.game_name == game_name)
        .order_by(desc(Games.score))
        .limit(limit)
    )
    result = await session.execute(query)
    return list(result.scalars().all())
