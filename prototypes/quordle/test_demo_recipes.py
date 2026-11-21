"""Проверки безопасного заполнения тестовой книги рецептов."""

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config_data.config import ROOT, Config, DataBase, Redis, TgBot
from database.models import Base, Cookbook, CookbookEnglish
from database.orm_cookbook_translations import orm_list_pending_translations

from .demo_recipes import seed_demo_recipes


class DemoRecipesTest(unittest.IsolatedAsyncioTestCase):
    """Проверяет, что фикстуры не перезаписывают старые записи."""

    async def test_seed_only_empty_test_cookbook(self) -> None:
        """Заполняет книгу и не требует платного перевода готовых рецептов."""
        with tempfile.TemporaryDirectory(dir=ROOT / "local_testing/data") as folder:
            path = Path(folder) / "fixture.sqlite3"
            url = f"sqlite+aiosqlite:///{path.as_posix()}"
            config = Config(TgBot("", ""), DataBase("", ""), Redis(), database_url=url)
            engine = create_async_engine(url)
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                self.assertEqual(await seed_demo_recipes(config), 5)
                self.assertEqual(await seed_demo_recipes(config), 0)
                sessions = async_sessionmaker(engine)
                async with sessions() as session:
                    for model in (Cookbook, CookbookEnglish):
                        self.assertEqual(
                            await session.scalar(select(func.count()).select_from(model)), 5
                        )
                    self.assertEqual(await orm_list_pending_translations(session), [])
                config.mode = "production"
                with self.assertRaises(ValueError):
                    await seed_demo_recipes(config)
            finally:
                await engine.dispose()
