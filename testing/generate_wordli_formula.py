"""Создаёт готовые изображения формул без зависимости от браузерных шрифтов."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SCALE = 4
COLOR = "#f5f5f5"
ROOT = Path(__file__).resolve().parents[1] / "prototypes" / "wordli"


def render_images(font_path: Path, font_index: int) -> None:
    """Рисует прозрачные PNG с запасом разрешения для мобильного экрана."""
    font = ImageFont.truetype(str(font_path), 28 * SCALE, index=font_index)
    small = ImageFont.truetype(str(font_path), 23 * SCALE, index=font_index)

    def text(draw: ImageDraw.ImageDraw, x: float, y: float, value: str) -> float:
        """Размещает символы относительно середины математической строки."""
        draw.text((x * SCALE, y * SCALE), value, font=font, fill=COLOR, anchor="lm")
        return x + draw.textlength(value, font=font) / SCALE

    def fraction(draw: ImageDraw.ImageDraw, x: float, numerator: str,
                 denominator: str, y: float = 39) -> float:
        """Рисует дробь с явными отступами от числителя и знаменателя."""
        width = max(draw.textlength(value, font=small) / SCALE
                    for value in (numerator, denominator)) + 9
        middle = (x + width / 2) * SCALE
        draw.text((middle, (y - 14) * SCALE), numerator, font=small,
                  fill=COLOR, anchor="mm")
        draw.text((middle, (y + 17) * SCALE), denominator, font=small,
                  fill=COLOR, anchor="mm")
        draw.line((x * SCALE, y * SCALE, (x + width) * SCALE, y * SCALE),
                  fill=COLOR, width=SCALE)
        return x + width

    def radical(draw: ImageDraw.ImageDraw, x: float, numerator: str) -> float:
        """Рисует корень с отдельным пространством над дробью."""
        end = fraction(draw, x + 23, numerator, "K")
        points = [(x, 45), (x + 5, 42), (x + 11, 61),
                  (x + 21, 8), (end + 4, 8)]
        draw.line([(a * SCALE, b * SCALE) for a, b in points],
                  fill=COLOR, width=round(1.3 * SCALE), joint="curve")
        return end + 9

    image = Image.new("RGBA", (700 * SCALE, 82 * SCALE))
    draw = ImageDraw.Draw(image)
    x = text(draw, 5, 40, "10000 · ")
    x = radical(draw, x, "N")
    x = text(draw, x, 40, " · P · ")
    # Скобки охватывают всю высоту дроби и не зависят от MathML WebView.
    draw.arc((x * SCALE, 4 * SCALE, (x + 21) * SCALE, 76 * SCALE),
             105, 255, fill=COLOR, width=round(1.3 * SCALE))
    x = text(draw, x + 19, 40, "0,4 + 0,4Q + 0,2 ")
    x = radical(draw, x, "M")
    draw.arc(((x - 3) * SCALE, 4 * SCALE, (x + 18) * SCALE, 76 * SCALE),
             -75, 75, fill=COLOR, width=round(1.3 * SCALE))
    image = image.crop((0, 0, round((x + 23) * SCALE), 82 * SCALE))

    destination = ROOT / "assets"
    destination.mkdir(exist_ok=True)
    image.save(destination / "score-formula.png", optimize=True)

    image = Image.new("RGBA", (180 * SCALE, 82 * SCALE))
    draw = ImageDraw.Draw(image)
    quality_font = ImageFont.truetype(str(font_path), 32 * SCALE, index=font_index)
    draw.text((2 * SCALE, 40 * SCALE), "Q = ", font=quality_font,
              fill=COLOR, anchor="lm")
    x = 2 + draw.textlength("Q = ", font=quality_font) / SCALE
    x = fraction(draw, x, "7 − A", "6")
    image.crop((0, 8 * SCALE, round((x + 3) * SCALE), 74 * SCALE)).save(
        destination / "quality-formula.png", optimize=True
    )


def main() -> None:
    """Принимает путь к шрифту для воспроизводимой сборки изображений."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path,
                        default=Path("C:/Windows/Fonts/cambria.ttc"))
    parser.add_argument("--font-index", type=int, default=1)
    args = parser.parse_args()
    render_images(args.font, args.font_index)


if __name__ == "__main__":
    main()
