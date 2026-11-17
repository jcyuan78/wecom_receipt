"""Tests for receipt image rectification."""

from __future__ import annotations

from PIL import Image, ImageDraw

from app.imaging import estimate_skew_angle, rectify_document


def _page(width: int = 800, height: int = 1100) -> Image.Image:
    page = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(page)
    draw.rectangle([40, 40, width - 40, 110], fill=(15, 15, 15))
    for index in range(12):
        top = 170 + index * 65
        draw.rectangle([70, top, width - 70, top + 26], fill=(45, 45, 45))
    return page


def _photo(angle: float = -7.0) -> Image.Image:
    page = _page()
    rotated = page.rotate(
        angle, expand=True, resample=Image.Resampling.BICUBIC, fillcolor="white"
    )
    mask = Image.new("L", page.size, 255).rotate(
        angle, expand=True, resample=Image.Resampling.BICUBIC, fillcolor=0
    )
    canvas = Image.new("RGB", (1300, 1600), (30, 35, 40))
    canvas.paste(rotated, ((1300 - rotated.width) // 2, (1600 - rotated.height) // 2), mask)
    return canvas


def test_photo_is_warped_cropped_and_deskewed():
    result = rectify_document(_photo())
    assert result.warped is True
    width, height = result.output_size
    assert height > width
    assert 1.2 < height / width < 1.6
    # The perspective warp removes the rotation; nothing should be left to deskew.
    assert estimate_skew_angle(result.image) < 1.5
    # The dark desk must be gone after the perspective warp: corners are paper.
    for point in ((1, 1), (width - 2, 1), (1, height - 2), (width - 2, height - 2)):
        assert min(result.image.getpixel(point)) > 200


def test_full_bleed_scan_is_deskewed():
    scan = _page(900, 1200).rotate(
        6, expand=True, resample=Image.Resampling.BICUBIC, fillcolor="white"
    )
    result = rectify_document(scan)
    assert abs(result.deskew_angle) > 3
    assert estimate_skew_angle(result.image) < 1.5


def test_small_rotation_is_left_alone():
    page = _page()
    result = rectify_document(page)
    assert abs(result.deskew_angle) < 0.5
    assert result.output_size[1] > result.output_size[0]


def test_output_is_resized():
    big = _page(2400, 3300)
    result = rectify_document(big, max_side=1400)
    assert max(result.output_size) <= 1400
