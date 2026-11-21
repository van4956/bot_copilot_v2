"""Добавление учебных рецептов в пустую локальную книгу Copilot."""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config_data.config import ROOT, Config, validate_test_database
from database.models import Cookbook, CookbookEnglish
from database.orm_cookbook_translations import recipe_source_hash

FIXTURE = ROOT / "local_testing/testing/fixtures/recipes.json"


async def seed_demo_recipes(config: Config) -> int:
    """Один раз заполняет пустую тестовую книгу без изменения других таблиц."""
    if not config.test_mode:
        raise ValueError("Учебные рецепты разрешены только для тестового Copilot.")
    validate_test_database(config.database_url)
    engine = create_async_engine(config.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session, session.begin():
            if await session.scalar(select(Cookbook.recipe_id).limit(1)) is not None:
                return 0
            recipes = json.loads(FIXTURE.read_text(encoding="utf-8"))
            session.add_all(Cookbook(**item) for item in recipes)
            await session.flush()
            session.add_all(
                CookbookEnglish(
                    recipe_id=item["recipe_id"],
                    recipe_name=f"Test recipe {number}",
                    description=f"Sample description for test recipe {number}.",
                    source_hash=recipe_source_hash(
                        item["recipe_name"], item["description"]
                    ),
                    model="local-test-fixture",
                )
                for number, item in enumerate(recipes, 1)
            )
            return len(recipes)
    finally:
        await engine.dispose()
