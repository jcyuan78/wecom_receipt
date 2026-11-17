"""Generate synthetic invoice images for testing the pipeline.

    .venv\\Scripts\\python.exe tools\\make_test_invoice.py
    .venv\\Scripts\\python.exe tools\\make_test_invoice.py --two

Then:
    .venv\\Scripts\\python.exe tools\\check_ai.py --image data\\test_invoice.jpg
    .venv\\Scripts\\python.exe tools\\process_image.py --file data\\test_invoices_two.jpg
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

ROOT = Path(__file__).resolve().parents[1]


def invoice_lines(store: str, number: str, date_text: str, total: str) -> list[tuple[str, int]]:
    return [
        (store, 46),
        ("INVOICE", 34),
        (f"No. {number}", 26),
        (f"Date: {date_text}", 26),
        ("", 16),
        ("Item                 Qty      Amount", 24),
        ("Notebook              2        1,200", 24),
        ("Pen                   1          800", 24),
        ("", 16),
        ("Subtotal                       2,000", 24),
        ("Tax                              298", 24),
        (f"TOTAL                    {total} JPY", 30),
    ]


def render(lines: list[tuple[str, int]], width: int = 900, height: int = 1250) -> Image.Image:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    y = 90
    for text, size in lines:
        if text:
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:  # Pillow < 10
                font = ImageFont.load_default()
            draw.text((80, y), text, fill=(10, 10, 10), font=font)
        y += int(size * 1.7) + 12
    draw.rectangle([60, 70, width - 60, height - 70], outline=(120, 120, 120), width=2)
    return image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--two", action="store_true", help="生成一张包含两张发票的图片")
    args = parser.parse_args()

    first = render(invoice_lines("TEST STORE", "INV-2026-001", "2026-01-15", "3,280"))

    if not args.two:
        output = ROOT / "data" / "test_invoice.jpg"
        output.parent.mkdir(parents=True, exist_ok=True)
        first.save(output, quality=92)
        print(f"已生成 {output}")
        return

    second = render(invoice_lines("SECOND SHOP", "INV-2026-002", "2026-01-16", "5,500"))
    canvas = Image.new("RGB", (1960, 1420), (28, 33, 38))
    canvas.paste(first, (40, 85))
    canvas.paste(second, (1020, 85))
    output = ROOT / "data" / "test_invoices_two.jpg"
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, quality=92)
    print(f"已生成 {output}（两张发票）")


if __name__ == "__main__":
    main()
