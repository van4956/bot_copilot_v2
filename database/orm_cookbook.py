"""Операции с рецептами без изменения их постоянных идентификаторов."""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Cookbook, CookbookEnglish


def _recipe_values(data: dict[str, Any]) -> dict[str, Any]:
    """Выбирает поля рецепта из пользовательского состояния."""
    return {
        "recipe_name": data["recipe_name"],
        "author": data["author"],
        "description": data["description"],
        "price": float(data["price"]),
        "image": data["image"],
    }


async def orm_add_recipe(
    session: AsyncSession, data: dict[str, Any]
) -> Cookbook:
    """Добавляет рецепт и возвращает созданный объект."""
    recipe = Cookbook(**_recipe_values(data))
    session.add(recipe)
    await session.commit()
    return recipe


async def orm_get_recipes(session: AsyncSession) -> list[Cookbook]:
    """Возвращает рецепты в устойчивом порядке."""
    result = await session.execute(select(Cookbook).order_by(Cookbook.recipe_id))
    return list(result.scalars().all())


async def orm_get_recipe(
    session: AsyncSession, recipe_id: int
) -> Cookbook | None:
    """Возвращает рецепт по постоянному идентификатору."""
    return await session.get(Cookbook, recipe_id)


async def orm_update_recipe(
    session: AsyncSession, recipe_id: int, data: dict[str, Any]
) -> bool:
    """Обновляет существующий рецепт без создания нового вместо удалённого."""
    result = await session.execute(
        update(Cookbook)
        .where(Cookbook.recipe_id == recipe_id)
        .values(**_recipe_values(data))
    )
    await session.commit()
    return result.rowcount > 0


async def orm_delete_recipe(session: AsyncSession, recipe_id: int) -> bool:
    """Удаляет рецепт, сохраняя идентификаторы остальных рецептов."""
    try:
        result = await session.execute(
            delete(Cookbook).where(Cookbook.recipe_id == recipe_id)
        )
        await session.execute(
            delete(CookbookEnglish).where(CookbookEnglish.recipe_id == recipe_id)
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return result.rowcount > 0
