"""Document detection and rectification.

A single photo may contain one receipt or several receipts laid out on a
desk.  ``detect_document_quads`` finds the paper regions, ``segment_documents``
returns one rectified image per region, and ``rectify_document`` keeps the old
single-document behaviour.
"""

from __future__ import annotations

import io
from loguru import logger as log
import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps


Image.MAX_IMAGE_PIXELS = 80_000_000


@dataclass(slots=True)
class RectifyResult:
    image: Image.Image
    warped: bool
    deskew_angle: float
    source_size: tuple[int, int]
    output_size: tuple[int, int]


def load_image(source: bytes | str | Path) -> Image.Image:
    if isinstance(source, (str, Path)):
        data = Path(source).read_bytes()
    else:
        data = source
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)
    return image.convert("RGB")


def encode_jpeg(image: Image.Image, quality: int = 92) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def prepare_for_model(image: Image.Image, max_side: int = 1280) -> bytes:
    copy = image.copy()
    copy.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return encode_jpeg(copy, quality=88)


def _order_points(points: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype="float32")
    sums = points.sum(axis=1)
    rect[0] = points[np.argmin(sums)]
    rect[2] = points[np.argmax(sums)]
    diffs = np.diff(points, axis=1)
    rect[1] = points[np.argmin(diffs)]
    rect[3] = points[np.argmax(diffs)]
    return rect


def _quad_area(quad: np.ndarray) -> float:
    return float(abs(cv2.contourArea(quad.astype(np.float32))))


def _resize_for_detection(gray: np.ndarray, max_side: int = 1600) -> tuple[np.ndarray, float]:
    height, width = gray.shape[:2]
    scale = min(1.0, max_side / max(height, width))
    if scale >= 1.0:
        return gray, 1.0
    resized = cv2.resize(
        gray, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA
    )
    return resized, scale


def _paper_quads(gray: np.ndarray) -> list[np.ndarray]:
    """Bright, page-like blobs (photos of paper on a darker desk)."""
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    quads: list[np.ndarray] = []
    image_area = float(gray.shape[0] * gray.shape[1])
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < image_area * 0.04:
            continue
        rect = cv2.minAreaRect(contour)
        (width, height) = rect[1]
        if min(width, height) < 20:
            continue
        ratio = max(width, height) / max(1.0, min(width, height))
        if ratio > 4.0:
            continue
        quads.append(cv2.boxPoints(rect).astype("float32"))
    return quads


def _edge_quads(gray: np.ndarray) -> list[np.ndarray]:
    """Rectangular contours found on strong edges (scans, bordered pages)."""
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    image_area = float(gray.shape[0] * gray.shape[1])
    quads: list[np.ndarray] = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:12]:
        area = cv2.contourArea(contour)
        if area < image_area * 0.05:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        quads.append(approx.reshape(4, 2).astype("float32"))
    return quads


def _bounding_iou(first: np.ndarray, second: np.ndarray) -> float:
    ax, ay, aw, ah = cv2.boundingRect(first.astype(np.float32))
    bx, by, bw, bh = cv2.boundingRect(second.astype(np.float32))
    left = max(ax, bx)
    top = max(ay, by)
    right = min(ax + aw, bx + bw)
    bottom = min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union else 0.0


def _contains(outer: np.ndarray, inner: np.ndarray) -> bool:
    center = inner.mean(axis=0)
    result = cv2.pointPolygonTest(
        outer.astype(np.float32).reshape(-1, 1, 2),
        (float(center[0]), float(center[1])),
        False,
    )
    return result >= 0


def _select_quads(candidates: list[np.ndarray], max_regions: int) -> list[np.ndarray]:
    ranked = sorted(
        ((_quad_area(quad), quad) for quad in candidates), key=lambda item: item[0], reverse=True
    )
    accepted: list[np.ndarray] = []
    for area, quad in ranked:
        if area < 100.0:
            continue
        if any(_bounding_iou(quad, other) > 0.5 for other in accepted):
            continue
        if any(_contains(other, quad) and area < _quad_area(other) * 0.9 for other in accepted):
            continue
        accepted.append(quad)
        if len(accepted) >= max_regions:
            break

    accepted.sort(key=lambda q: (float(q[:, 1].min()), float(q[:, 0].min())))
    return accepted


def detect_document_quads(image: Image.Image, max_regions: int = 6) -> list[np.ndarray]:
    """Return up to ``max_regions`` document quads, ordered top-left first."""
    gray = np.array(image.convert("L"))
    small, scale = _resize_for_detection(gray)
    area = float(small.shape[0] * small.shape[1])

    candidates = _paper_quads(small) + _edge_quads(small)
    selected: list[np.ndarray] = []
    for quad in _select_quads(candidates, max_regions):
        quad_area = _quad_area(quad)
        if quad_area > area * 0.985:
            # The whole frame is paper: treat it as one full-page document.
            continue
        selected.append(quad / scale)
    return selected


def _perspective_warp(image: Image.Image, quad: np.ndarray) -> Image.Image | None:
    # Canny + dilation makes the detected quad a few pixels larger than the
    # paper; shrink it slightly so no dark desk border leaks into the result.
    center = quad.mean(axis=0)
    quad = center + (quad - center) * 0.995

    top_left, top_right, bottom_right, bottom_left = _order_points(quad)
    width = max(
        np.linalg.norm(bottom_right - bottom_left),
        np.linalg.norm(top_right - top_left),
    )
    height = max(
        np.linalg.norm(top_right - bottom_right),
        np.linalg.norm(top_left - bottom_left),
    )
    width, height = int(round(width)), int(round(height))
    if width < 200 or height < 200:
        return None

    destination = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype="float32",
    )
    matrix = cv2.getPerspectiveTransform(
        np.array([top_left, top_right, bottom_right, bottom_left], dtype="float32"),
        destination,
    )
    warped = cv2.warpPerspective(np.array(image), matrix, (width, height))
    return Image.fromarray(warped)


def estimate_skew_angle(image: Image.Image, max_angle: float = 20.0) -> float:
    """Estimate the dominant text-line angle in degrees (positive = clockwise)."""
    gray = np.array(image.convert("L"))
    height, width = gray.shape[:2]
    if max(height, width) > 2000:
        scale = 2000.0 / max(height, width)
        gray = cv2.resize(gray, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)

    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    min_length = max(40, gray.shape[1] // 5)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=80,
        minLineLength=min_length,
        maxLineGap=20,
    )
    if lines is None:
        return 0.0

    angles: list[float] = []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        if x2 == x1:
            continue
        angle = math.degrees(math.atan2(float(y2 - y1), float(x2 - x1)))
        if abs(angle) <= max_angle:
            angles.append(angle)
    if not angles:
        return 0.0

    angle = float(np.median(angles))
    return 0.0 if abs(angle) < 0.2 else angle


def _crop_to_content(image: Image.Image) -> Image.Image:
    """Remove near-white margins around the actual printed content."""
    gray = np.array(image.convert("L"))
    mask = gray < 235
    if mask.sum() < mask.size * 0.002:
        return image

    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return image

    height, width = gray.shape[:2]
    top, bottom = int(rows[0]), int(rows[-1])
    left, right = int(cols[0]), int(cols[-1])

    # Never crop more than 15% on any side; protects against faint edges.
    margin_y = int(height * 0.01)
    margin_x = int(width * 0.01)
    top = max(0, top - margin_y)
    left = max(0, left - margin_x)
    bottom = min(height - 1, bottom + margin_y)
    right = min(width - 1, right + margin_x)

    if (right - left) < width * 0.5 or (bottom - top) < height * 0.5:
        return image
    return image.crop((left, top, right + 1, bottom + 1))


def deskew(image: Image.Image, angle: float | None = None) -> tuple[Image.Image, float]:
    angle = estimate_skew_angle(image) if angle is None else angle
    if abs(angle) < 0.2:
        return image, 0.0
    rotated = image.rotate(
        angle,
        resample=Image.Resampling.BICUBIC,
        expand=True,
        fillcolor=(255, 255, 255),
    )
    return rotated, angle


def _finish(image: Image.Image, warped: bool, source_size: tuple[int, int], max_side: int) -> RectifyResult:
    working = _crop_to_content(image)
    working, angle = deskew(working)
    working = _crop_to_content(working)

    if max(working.size) > max_side:
        working.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    return RectifyResult(
        image=working,
        warped=warped,
        deskew_angle=angle,
        source_size=source_size,
        output_size=working.size,
    )


def _rectify_with_quad(
    original: Image.Image, quad: np.ndarray, max_side: int
) -> RectifyResult | None:
    warped = _perspective_warp(original, quad)
    if warped is None:
        return None
    return _finish(warped, True, original.size, max_side)


def rectify_document(image: Image.Image, max_side: int = 2200) -> RectifyResult:
    """Rectify the largest document in the image (single-invoice behaviour)."""
    original = ImageOps.exif_transpose(image).convert("RGB")
    quads = detect_document_quads(original, max_regions=1)
    if quads:
        result = _rectify_with_quad(original, quads[0], max_side)
        if result is not None:
            log.info(
                "发票矫正: {} -> {}, 透视=True, 倾斜={:.1f}°",
                original.size,
                result.output_size,
                result.deskew_angle,
            )
            return result

    result = _finish(original, False, original.size, max_side)
    log.info(
        "发票矫正: {} -> {}, 透视=False, 倾斜={:.1f}°",
        original.size,
        result.output_size,
        result.deskew_angle,
    )
    return result


def segment_documents(
    image: Image.Image, max_side: int = 2200, max_regions: int = 6
) -> list[RectifyResult]:
    """Rectify every detected receipt in the image.

    Falls back to a single full-frame result when no document is detected.
    """
    original = ImageOps.exif_transpose(image).convert("RGB")
    quads = detect_document_quads(original, max_regions=max_regions)
    if not quads:
        return [_finish(original, False, original.size, max_side)]

    results: list[RectifyResult] = []
    for quad in quads:
        result = _rectify_with_quad(original, quad, max_side)
        if result is not None:
            results.append(result)

    if not results:
        return [_finish(original, False, original.size, max_side)]

    log.info(
        "检测到 {} 张票据（原图 {}），输出尺寸 {}",
        len(results),
        original.size,
        [item.output_size for item in results],
    )
    return results
