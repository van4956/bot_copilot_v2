"""Проверки генератора учебных рецептов без запуска бота и БД."""

import unittest
from decimal import Decimal

from prototypes.quordle.demo_cookbook_data import (
    DEMO_AUTHOR,
    generate_demo_recipes,
)


class DemoCookbookDataTests(unittest.TestCase):
    """Проверяет воспроизводимость, варианты и ограничения модели книги."""

    def test_same_seed_repeats_result(self) -> None:
        """Одинаковые параметры воспроизводят даже цены и изображения."""
        first = generate_demo_recipes(42)
        self.assertEqual(len(first), 12)
        self.assertEqual(first, generate_demo_recipes(42))
        self.assertNotEqual(
            {recipe.recipe_name for recipe in first},
            {recipe.recipe_name for recipe in generate_demo_recipes(99)},
        )

    def test_fixture_fields(self) -> None:
        """Все записи самостоятельны, двуязычны и помещаются в схему."""
        expected_images = {f"test-fixture:{i}" for i in range(1, 6)}
        for count in range(10, 15):
            with self.subTest(count=count):
                book = generate_demo_recipes(180, count)
                self.assertEqual(count, len(book))
                self.assertEqual(count, len({r.recipe_name for r in book}))
                self.assertEqual(count, len({r.recipe_name_en for r in book}))
                self.assertEqual({r.image for r in book}, expected_images)
                for recipe in book:
                    self.assertLessEqual(len(recipe.recipe_name), 150)
                    self.assertLessEqual(len(recipe.recipe_name_en), 300)
                    self.assertEqual(recipe.author, DEMO_AUTHOR)
                    self.assertIsInstance(recipe.price, Decimal)
                    self.assertGreater(recipe.price, 0)
                    self.assertRegex(recipe.description, r"[а-яё]")
                    self.assertRegex(recipe.description_en, r"[a-z]")
                    self.assertNotRegex(recipe.description_en, r"[а-яА-ЯёЁ]")

    def test_invalid_count(self) -> None:
        """Генератор отклоняет размеры книги вне согласованного диапазона."""
        for count in (9, 15):
            with self.subTest(count=count), self.assertRaises(ValueError):
                generate_demo_recipes(0, count)
