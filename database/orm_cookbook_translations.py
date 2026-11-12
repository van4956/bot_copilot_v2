"""Локализованные рецепты и атомарная публикация переводов."""

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Iterable

from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Cookbook, CookbookEnglish


@dataclass(frozen=True)
class RecipeSource:
    """Неизменяемый снимок переводимых русских полей."""

    recipe_id: int
    recipe_name: str
    description: str
    source_hash: str


@dataclass(frozen=True)
class RecipeTranslation:
    """Перевод конкретного снимка русского рецепта."""

    source: RecipeSource
    recipe_name: str
    description: str
    model: str


@dataclass(frozen=True)
class TranslationPublishResult:
    """Число опубликованных и пропущенных переводов."""

    saved: int = 0
    changed: int = 0
    deleted: int = 0


@dataclass(frozen=True)
class LocalizedRecipe:
    """Тексты выбранного языка с общими реквизитами рецепта."""

    recipe_id: int
    recipe_name: str
    author: str | None
    description: str
    price: float
    image: str | None
    translation_revision: str = ""


def recipe_source_hash(recipe_name: str, description: str) -> str:
    """Вычисляет версию только названия и описания."""
    value = json.dumps(
        [recipe_name, description], ensure_ascii=False, separators=(",", ":")
    )
    return sha256(value.encode("utf-8")).hexdigest()


def _source(recipe: Cookbook) -> RecipeSource:
    """Отделяет русский оригинал от изменяемого ORM-объекта."""
    return RecipeSource(
        recipe_id=recipe.recipe_id,
        recipe_name=recipe.recipe_name,
        description=recipe.description,
        source_hash=recipe_source_hash(recipe.recipe_name, recipe.description),
    )


async def orm_list_pending_translations(
    session: AsyncSession,
) -> list[RecipeSource]:
    """Возвращает новые и изменённые оригиналы в порядке книги."""
    rows = await session.execute(
        select(Cookbook, CookbookEnglish.source_hash)
        .outerjoin(
            CookbookEnglish, CookbookEnglish.recipe_id == Cookbook.recipe_id
        )
        .order_by(Cookbook.recipe_id)
        .execution_options(populate_existing=True)
    )
    pending = []
    for recipe, translated_hash in rows:
        source = _source(recipe)
        if source.source_hash != translated_hash:
            pending.append(source)
    return pending


async def orm_get_localized_recipes(
    session: AsyncSession, locale: str
) -> list[LocalizedRecipe]:
    """Выбирает EN, включая устаревший; без EN оставляет оригинал."""
    use_russian = locale.lower().replace("_", "-").split("-")[0] == "ru"
    rows = await session.execute(
        select(Cookbook, CookbookEnglish)
        .outerjoin(
            CookbookEnglish, CookbookEnglish.recipe_id == Cookbook.recipe_id
        )
        .order_by(Cookbook.recipe_id)
        .execution_options(populate_existing=True)
    )
    recipes = []
    for recipe, english in rows:
        text = recipe if use_russian or english is None else english
        language = "ru" if text is recipe else "en"
        revision = sha256(
            f"{language}:{recipe_source_hash(text.recipe_name, text.description)}".encode(
                "utf-8"
            )
        ).hexdigest()
        recipes.append(
            LocalizedRecipe(
                recipe_id=recipe.recipe_id,
                recipe_name=text.recipe_name,
                author=recipe.author,
                description=text.description,
                price=recipe.price,
                image=recipe.image,
                translation_revision=revision,
            )
        )
    return recipes


def _validate_translations(translations: list[RecipeTranslation]) -> None:
    """Отклоняет неоднозначные или повреждённые результаты перевода."""
    recipe_ids = [translation.source.recipe_id for translation in translations]
    if len(recipe_ids) != len(set(recipe_ids)):
        raise ValueError("Рецепт указан в пакете переводов повторно")
    for translation in translations:
        source = translation.source
        if source.source_hash != recipe_source_hash(
            source.recipe_name, source.description
        ):
            raise ValueError("Хеш оригинала не соответствует снимку рецепта")
        if (
            not isinstance(translation.recipe_name, str)
            or not translation.recipe_name.strip()
            or len(translation.recipe_name) > 300
        ):
            raise ValueError(
                "Название перевода должно содержать от 1 до 300 символов"
            )
        if (
            not isinstance(translation.description, str)
            or not translation.description.strip()
            or len(translation.description) > 12000
        ):
            raise ValueError(
                "Описание перевода должно содержать от 1 до 12000 символов"
            )
        if (
            not isinstance(translation.model, str)
            or not translation.model.strip()
            or len(translation.model) > 100
        ):
            raise ValueError(
                "Не указана модель перевода или её имя слишком длинное"
            )


async def orm_save_translations(
    session: AsyncSession, translations: Iterable[RecipeTranslation]
) -> TranslationPublishResult:
    """Атомарно публикует пакет, повторно проверяя русские оригиналы."""
    batch = list(translations)
    _validate_translations(batch)
    if not batch:
        return TranslationPublishResult()
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:
        raise ValueError("Переводы поддерживают SQLite и PostgreSQL")
    saved = changed = deleted = 0
    try:
        current = {
            recipe.recipe_id: recipe
            for recipe in await session.scalars(
                select(Cookbook)
                .where(
                    Cookbook.recipe_id.in_(
                        [translation.source.recipe_id for translation in batch]
                    )
                )
                .order_by(Cookbook.recipe_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
        for translation in batch:
            source = translation.source
            recipe = current.get(source.recipe_id)
            if recipe is None:
                deleted += 1
                continue
            if _source(recipe).source_hash != source.source_hash:
                changed += 1
                continue
            values = {
                "recipe_name": translation.recipe_name,
                "description": translation.description,
                "source_hash": source.source_hash,
                "model": translation.model,
            }
            # Условная вставка защищает и SQLite без блокировок SELECT FOR UPDATE.
            query = insert(CookbookEnglish).from_select(
                ["recipe_id", *values],
                select(
                    literal(source.recipe_id),
                    *(literal(value) for value in values.values()),
                )
                .select_from(Cookbook)
                .where(
                    Cookbook.recipe_id == source.recipe_id,
                    Cookbook.recipe_name == source.recipe_name,
                    Cookbook.description == source.description,
                ),
            )
            query = query.on_conflict_do_update(
                index_elements=[CookbookEnglish.recipe_id],
                set_={**values, "updated": func.now()},
            ).returning(CookbookEnglish.recipe_id)
            if await session.scalar(query) is not None:
                saved += 1
            elif (
                await session.scalar(
                    select(Cookbook.recipe_id).where(
                        Cookbook.recipe_id == source.recipe_id
                    )
                )
                is None
            ):
                deleted += 1
            else:
                changed += 1
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return TranslationPublishResult(
        saved=saved, changed=changed, deleted=deleted
    )
