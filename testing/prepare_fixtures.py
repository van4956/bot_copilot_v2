"""Создание искусственных JSON-данных и изображений для локальных тестов."""

import argparse
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "testing" / "fixtures"
USER_ID_BASE = 9_000_000_000
RECORD_ID_BASE = 9_000_000


def build_fixture_records() -> dict[str, list[dict[str, Any]]]:
    """Формирует устойчивый набор вымышленных пользователей, рецептов и игр."""
    users = [
        {
            "user_id": USER_ID_BASE + number,
            "user_name": (
                "" if number % 5 == 0 else f"fixture_user_{number:03d}"
            ),
            "full_name": f"Тестовый пользователь {number:03d}",
            "locale": "ru" if number % 2 else "en",
            "status": "kicked" if number % 10 == 0 else "member",
            "flag": 1,
        }
        for number in range(1, 121)
    ]
    recipes = [
        {
            "recipe_id": RECORD_ID_BASE + number,
            "recipe_name": f"Тестовый рецепт {number}",
            "author": f"Тестовый автор {number}",
            "description": (
                f"Искусственное описание тестового рецепта {number}."
            ),
            "price": number * 10.5,
            "image": f"test-fixture:{number}",
        }
        for number in range(1, 6)
    ]
    games = [
        {
            "game_id": RECORD_ID_BASE + number,
            "game_name": "snake" if number % 2 else "platform",
            "user_id": USER_ID_BASE + number,
            "user_name": (
                None if number % 4 == 0 else users[number - 1]["user_name"]
            ),
            "score": ((number * 3) % 41) * 10,
        }
        for number in range(1, 31)
    ]
    return {"users": users, "recipes": recipes, "games": games}


def _fixture_font(font_path: Path | None) -> ImageFont.FreeTypeFont:
    """Выбирает шрифт с кириллицей без сетевой загрузки."""
    if font_path is not None:
        return ImageFont.truetype(str(font_path), 40)
    candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/Library/Fonts/Arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), 40)
    raise ValueError("Не найден шрифт с кириллицей; задайте --font")


def prepare_fixtures(
    output_dir: Path = FIXTURE_DIR, font_path: Path | None = None
) -> dict[str, int]:
    """Записывает JSON и пять воспроизводимых PNG с подписями."""
    font = _fixture_font(font_path)
    output_dir = Path(output_dir)
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    records = build_fixture_records()
    for name, rows in records.items():
        content = json.dumps(
            rows, ensure_ascii=False, indent=2, sort_keys=True
        )
        (output_dir / f"{name}.json").write_text(
            content + "\n", encoding="utf-8"
        )

    for number in range(1, 6):
        caption = f"тест картинка {number}"
        image = Image.new("RGB", (800, 600), "white")
        draw = ImageDraw.Draw(image)
        draw.text((400, 300), caption, fill="black", font=font, anchor="mm")
        metadata = PngImagePlugin.PngInfo()
        metadata.add_itxt("caption", caption)
        image.save(
            images_dir / f"test_image_{number}.png",
            format="PNG", pnginfo=metadata, compress_level=9, optimize=False,
        )
    return {name: len(rows) for name, rows in records.items()}


def main() -> None:
    """Запускает генерацию только локальных тестовых файлов."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=FIXTURE_DIR)
    parser.add_argument("--font", type=Path, default=None)
    args = parser.parse_args()
    counts = prepare_fixtures(args.output_dir, args.font)
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
