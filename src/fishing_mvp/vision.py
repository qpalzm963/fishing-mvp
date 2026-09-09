"""Resolution-independent OpenCV heuristics for the fishing UI.

The detector deliberately starts from visual primitives rather than fixed
click coordinates.  It finds the large action control first, then searches
for prompt and QTE elements in regions relative to that detected control.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np

from .config import DetectorConfig
from .models import Box, Detection, FishingState


@dataclass(frozen=True)
class _ButtonCandidate:
    box: Box
    radius: float
    score: float
    purple_ratio: float
    mean_saturation: float


@dataclass(frozen=True)
class _GaugeCandidate:
    box: Box
    score: float
    marker_x: float | None
    marker_width: float | None
    target_range: tuple[float, float] | None


@dataclass(frozen=True)
class _GaugeRefinement:
    """Colour geometry refined inside an original-resolution gauge ROI."""

    marker_x: float | None
    marker_width: float | None
    target_range: tuple[float, float] | None
    marker_pixels: int
    target_pixels: int


def _contours(mask: np.ndarray) -> list[np.ndarray]:
    found = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return list(found[0] if len(found) == 2 else found[1])


def _clip_box(x: int, y: int, w: int, h: int, frame_w: int, frame_h: int) -> Box:
    x0 = max(0, min(frame_w, int(x)))
    y0 = max(0, min(frame_h, int(y)))
    x1 = max(x0, min(frame_w, int(x + w)))
    y1 = max(y0, min(frame_h, int(y + h)))
    return Box(x0, y0, x1 - x0, y1 - y0)


def _crop(frame: np.ndarray, box: Box) -> np.ndarray:
    return frame[box.y : box.bottom, box.x : box.right]


def _overlap_ratio(first: Box | None, second: Box | None) -> float:
    if first is None or second is None:
        return 0.0
    x0 = max(first.x, second.x)
    y0 = max(first.y, second.y)
    x1 = min(first.right, second.right)
    y1 = min(first.bottom, second.bottom)
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    return intersection / max(1.0, min(first.area, second.area))


def _ratio(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return float(np.clip((value - low) / (high - low), 0.0, 1.0))


def _hsv_mask(hsv: np.ndarray, ranges: Iterable[tuple[int, int, int, int, int, int]]) -> np.ndarray:
    result = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for h_low, h_high, s_low, s_high, v_low, v_high in ranges:
        result = cv2.bitwise_or(
            result,
            cv2.inRange(hsv, (h_low, s_low, v_low), (h_high, s_high, v_high)),
        )
    return result


def color_mask(
    frame: np.ndarray,
    color: str,
    *,
    assume_hsv: bool = False,
    config: DetectorConfig | None = None,
) -> np.ndarray:
    """Return a named HSV mask; exposed for small deterministic unit tests."""

    hsv = frame if assume_hsv else cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    ranges: dict[str, tuple[tuple[int, int, int, int, int, int], ...]] = {
        "purple": ((125, 179, 45, 255, 45, 255),),
        "yellow": ((15, 45, 80, 255, 90, 255),),
        "red": ((0, 12, 75, 255, 70, 255), (168, 179, 75, 255, 70, 255)),
        "blue": ((88, 125, 95, 255, 110, 255),),
        "pink": ((145, 179, 75, 255, 100, 255),),
        "orange": ((3, 27, 95, 255, 100, 255),),
        "green": ((35, 95, 75, 255, 80, 255),),
    }
    if config is not None:
        ranges["purple"] = ((
            config.purple_hue_low,
            config.purple_hue_high,
            config.min_saturation,
            255,
            config.min_value,
            255,
        ),)
    if color not in ranges:
        raise ValueError(f"Unknown color mask: {color}")
    return _hsv_mask(hsv, ranges[color])


def _prepare_frame(frame: np.ndarray, max_width: int) -> tuple[np.ndarray, float]:
    height, width = frame.shape[:2]
    if max_width <= 0 or width <= max_width:
        return frame, 1.0
    scale = max_width / float(width)
    prepared = cv2.resize(frame, (max_width, int(round(height * scale))), interpolation=cv2.INTER_AREA)
    return prepared, scale


def _restore_box(box: Box | None, scale: float, frame_w: int, frame_h: int) -> Box | None:
    if box is None:
        return None
    if scale <= 0:
        return box
    return _clip_box(box.x / scale, box.y / scale, box.w / scale, box.h / scale, frame_w, frame_h)


def _button_colour_metrics(
    hsv: np.ndarray,
    cx: int,
    cy: int,
    radius: float,
    config: DetectorConfig,
) -> tuple[float, float]:
    height, width = hsv.shape[:2]
    r = max(2, int(radius * 0.92))
    box = _clip_box(cx - r, cy - r, 2 * r, 2 * r, width, height)
    crop = _crop(hsv, box)
    if crop.size == 0:
        return 0.0, 0.0
    yy, xx = np.ogrid[: crop.shape[0], : crop.shape[1]]
    center_x = crop.shape[1] / 2.0
    center_y = crop.shape[0] / 2.0
    circle = (xx - center_x) ** 2 + (yy - center_y) ** 2 <= (min(crop.shape[:2]) * 0.48) ** 2
    purple = color_mask(crop, "purple", assume_hsv=True, config=config) > 0
    purple_ratio = float(np.mean(purple[circle])) if np.any(circle) else 0.0
    mean_saturation = float(np.mean(crop[:, :, 1][circle])) if np.any(circle) else 0.0
    return purple_ratio, mean_saturation


def _score_button(width: int, height: int, cx: float, cy: float, radius: float, purple_ratio: float, sat: float) -> float:
    centrality = 1.0 - min(1.0, abs(cx / max(1.0, width) - 0.5) * 3.2)
    lower = _ratio(cy / max(1.0, height), 0.55, 0.93)
    radius_score = 1.0 - min(1.0, abs(radius / max(1.0, width) - 0.115) / 0.13)
    colour_score = min(1.0, purple_ratio / 0.25) * 0.65 + min(1.0, sat / 150.0) * 0.35
    return float(np.clip(0.36 * centrality + 0.28 * lower + 0.18 * radius_score + 0.18 * colour_score, 0.0, 1.0))


def detect_action_button(frame: np.ndarray, config: DetectorConfig) -> tuple[Box | None, bool, float, dict[str, float]]:
    """Find the large round action control without assuming its pixel position."""

    height, width = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (9, 9), 2)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    min_radius = max(8, int(width * config.button_min_radius_ratio))
    max_radius = max(min_radius + 4, int(width * config.button_max_radius_ratio))
    candidates: list[_ButtonCandidate] = []
    search_top = max(0, int(height * config.button_min_y_ratio - max_radius - 8))
    search_bottom = min(height, int(height * config.button_max_y_ratio + max_radius + 8))
    search_blurred = blurred[search_top:search_bottom, :]

    circles = cv2.HoughCircles(
        search_blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=max(12, int(width * 0.12)),
        param1=80,
        param2=27,
        minRadius=min_radius,
        maxRadius=max_radius,
    )
    if circles is not None:
        for raw_cx, raw_cy, raw_radius in np.round(circles[0]).astype(int):
            raw_cy += search_top
            y_ratio = raw_cy / max(1, height)
            if y_ratio < config.button_min_y_ratio or y_ratio > config.button_max_y_ratio:
                continue
            purple_ratio, sat = _button_colour_metrics(hsv, raw_cx, raw_cy, raw_radius, config)
            score = _score_button(width, height, raw_cx, raw_cy, raw_radius, purple_ratio, sat)
            box = _clip_box(
                raw_cx - raw_radius,
                raw_cy - raw_radius,
                raw_radius * 2,
                raw_radius * 2,
                width,
                height,
            )
            candidates.append(_ButtonCandidate(box, raw_radius, score, purple_ratio, sat))

    # A contour fallback handles anti-aliased rings that do not produce a
    # stable Hough circle on some devices or video codecs.
    edges = cv2.Canny(search_blurred, 45, 130)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    for contour in _contours(edges):
        area = cv2.contourArea(contour)
        if area < math.pi * min_radius * min_radius * 0.35:
            continue
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            continue
        circularity = 4.0 * math.pi * area / (perimeter * perimeter)
        if circularity < 0.45:
            continue
        (cx, cy), radius = cv2.minEnclosingCircle(contour)
        cy += search_top
        y_ratio = cy / max(1, height)
        if y_ratio < config.button_min_y_ratio or y_ratio > config.button_max_y_ratio or not (min_radius <= radius <= max_radius):
            continue
        purple_ratio, sat = _button_colour_metrics(hsv, int(cx), int(cy), radius, config)
        score = _score_button(width, height, cx, cy, radius, purple_ratio, sat) * min(1.0, circularity)
        box = _clip_box(cx - radius, cy - radius, radius * 2, radius * 2, width, height)
        candidates.append(_ButtonCandidate(box, radius, score, purple_ratio, sat))

    if not candidates:
        return None, False, 0.0, {"purple_ratio": 0.0, "saturation": 0.0}
    best = max(candidates, key=lambda candidate: candidate.score)
    active_score = min(1.0, best.purple_ratio / 0.30) * 0.72 + min(1.0, best.mean_saturation / 150.0) * 0.28
    active = best.purple_ratio >= config.button_active_purple_ratio or best.mean_saturation >= config.button_active_saturation
    return best.box, bool(active), float(best.score), {
        "purple_ratio": round(best.purple_ratio, 4),
        "saturation": round(best.mean_saturation, 2),
        "active_score": round(active_score, 4),
    }


def detect_action_button_nearby(
    frame: np.ndarray,
    previous: Box,
    config: DetectorConfig,
) -> tuple[Box | None, bool, float, dict[str, float]]:
    """Track a previously detected button through a small local ROI.

    QTE frames arrive at a higher cadence than idle frames.  Re-running the
    full-frame Hough search for every QTE frame adds avoidable latency, while
    the action control itself normally remains stable.  This fast path still
    verifies the circle from the current pixels and falls back to the full
    detector when the local track is not convincing.
    """

    height, width = frame.shape[:2]
    previous_radius = max(8.0, min(previous.w, previous.h) / 2.0)
    expand = previous_radius * 1.45
    roi = _clip_box(
        previous.cx - expand,
        previous.cy - expand,
        expand * 2.0,
        expand * 2.0,
        width,
        height,
    )
    crop = _crop(frame, roi)
    if crop.size == 0:
        return None, False, 0.0, {"purple_ratio": 0.0, "saturation": 0.0}
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (7, 7), 1.5)
    circles = cv2.HoughCircles(
        blurred,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=max(8, int(previous_radius * 0.85)),
        param1=70,
        param2=22,
        minRadius=max(8, int(previous_radius * 0.62)),
        maxRadius=max(9, int(previous_radius * 1.38)),
    )
    if circles is None:
        return None, False, 0.0, {"purple_ratio": 0.0, "saturation": 0.0}
    candidates: list[_ButtonCandidate] = []
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    for raw_cx, raw_cy, raw_radius in np.round(circles[0]).astype(int):
        cx = raw_cx + roi.x
        cy = raw_cy + roi.y
        radius = float(raw_radius)
        if abs(cx - previous.cx) > previous_radius * 0.70 or abs(cy - previous.cy) > previous_radius * 0.70:
            continue
        purple_ratio, sat = _button_colour_metrics(hsv, cx, cy, radius, config)
        score = _score_button(width, height, cx, cy, radius, purple_ratio, sat)
        box = _clip_box(cx - radius, cy - radius, radius * 2, radius * 2, width, height)
        candidates.append(_ButtonCandidate(box, radius, score, purple_ratio, sat))
    if not candidates:
        return None, False, 0.0, {"purple_ratio": 0.0, "saturation": 0.0}
    best = min(candidates, key=lambda candidate: abs(candidate.box.cx - previous.cx) + abs(candidate.box.cy - previous.cy))
    active_score = min(1.0, best.purple_ratio / 0.30) * 0.72 + min(1.0, best.mean_saturation / 150.0) * 0.28
    active = best.purple_ratio >= config.button_active_purple_ratio or best.mean_saturation >= config.button_active_saturation
    return best.box, bool(active), float(best.score), {
        "purple_ratio": round(best.purple_ratio, 4),
        "saturation": round(best.mean_saturation, 2),
        "active_score": round(active_score, 4),
    }


def _relative_roi(
    center_x: float,
    top: float,
    right: float,
    bottom: float,
    width: int,
    height: int,
) -> Box:
    return _clip_box(center_x - right, top, right * 2, bottom - top, width, height)


def detect_prompt(frame: np.ndarray, button: Box | None, config: DetectorConfig) -> tuple[Box | None, float]:
    if button is None:
        return None, 0.0
    height, width = frame.shape[:2]
    radius = max(4.0, min(button.w, button.h) / 2.0)
    roi = _relative_roi(
        button.cx,
        button.y - radius * 2.6,
        radius * 3.2,
        button.y - radius * 0.35,
        width,
        height,
    )
    hsv = cv2.cvtColor(_crop(frame, roi), cv2.COLOR_BGR2HSV)
    mask = color_mask(hsv, "purple", assume_hsv=True, config=config)
    kernel_size = max(3, int(round(radius * 0.08)) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((kernel_size, kernel_size), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    candidates: list[tuple[float, Box]] = []
    for contour in _contours(mask):
        x, y, w, h = cv2.boundingRect(contour)
        if w <= 0 or h <= 0:
            continue
        aspect = w / float(h)
        fill = cv2.contourArea(contour) / float(w * h)
        if not (config.prompt_min_aspect <= aspect <= config.prompt_max_aspect):
            continue
        if w < radius * 1.2 or h < radius * 0.08 or fill < config.prompt_min_fill_ratio:
            continue
        absolute = Box(roi.x + x, roi.y + y, w, h)
        center_score = 1.0 - min(1.0, abs(absolute.cx - button.cx) / max(1.0, radius * 2.5))
        aspect_score = 1.0 - min(1.0, abs(aspect - 5.0) / 7.0)
        score = float(np.clip(0.50 * fill + 0.30 * center_score + 0.20 * aspect_score, 0.0, 1.0))
        candidates.append((score, absolute))
    if not candidates:
        return None, 0.0
    return max(candidates, key=lambda item: item[0])[1], max(candidates, key=lambda item: item[0])[0]


def _find_horizontal_colour_region(
    mask: np.ndarray,
    roi: Box,
    min_aspect: float,
    min_width: float,
    min_pixels: int,
) -> tuple[Box | None, float]:
    clean = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 11), np.uint8))
    candidates: list[tuple[float, Box, int]] = []
    for contour in _contours(clean):
        x, y, w, h = cv2.boundingRect(contour)
        pixels = int(cv2.countNonZero(mask[y : y + h, x : x + w]))
        if pixels < min_pixels or h <= 0 or w / float(h) < min_aspect or w < min_width:
            continue
        box = Box(roi.x + x, roi.y + y, w, h)
        aspect_score = min(1.0, (w / float(h) - min_aspect) / 8.0 + 0.35)
        density = min(1.0, pixels / max(1.0, w * h * 0.45))
        candidates.append((float(0.65 * aspect_score + 0.35 * density), box, pixels))
    if not candidates:
        return None, 0.0
    score, box, _ = max(candidates, key=lambda item: item[0])
    return box, float(np.clip(score, 0.0, 1.0))


def _compact_component_pixels(mask: np.ndarray) -> tuple[int, float]:
    """Count compact colour components while excluding a connected background."""

    height, width = mask.shape[:2]
    total = 0
    largest = 0.0
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    for index in range(1, count):
        x, y, w, h, area = [int(item) for item in stats[index]]
        if area < 10:
            continue
        touches_border = x <= 0 or y <= 0 or x + w >= width - 1 or y + h >= height - 1
        if touches_border or area > width * height * 0.18:
            continue
        total += area
        largest = max(largest, float(area))
    return total, largest


def _target_range_from_yellow(
    yellow_region: np.ndarray,
    gauge_width: int,
    config: DetectorConfig,
) -> tuple[float, float] | None:
    """Estimate a connected yellow target span in gauge-relative coordinates."""

    if gauge_width <= 1 or yellow_region.size == 0:
        return None
    points = cv2.findNonZero(yellow_region)
    minimum_pixels = max(3, int(config.gauge_target_min_color_pixels))
    if points is None or len(points) < minimum_pixels:
        return None

    coordinates = points.reshape(-1, 2)
    x_pixels = coordinates[:, 0].astype(np.float32)
    low_px = max(0, int(math.floor(float(np.quantile(x_pixels, 0.05)))))
    high_px = min(gauge_width - 1, int(math.ceil(float(np.quantile(x_pixels, 0.95)))))
    if high_px < low_px:
        return None

    span_pixels = high_px - low_px + 1
    minimum_span = max(
        float(max(1, int(config.gauge_target_min_width_px))),
        gauge_width * max(0.0, float(config.gauge_target_min_width_ratio)),
    )
    if span_pixels < minimum_span:
        return None

    occupied_columns = np.count_nonzero(yellow_region[:, low_px : high_px + 1], axis=0) > 0
    coverage = float(np.mean(occupied_columns)) if occupied_columns.size else 0.0
    if coverage < max(0.0, float(config.gauge_target_min_column_coverage)):
        return None

    denominator = float(max(1, gauge_width - 1))
    return (
        float(np.clip(low_px / denominator, 0.0, 1.0)),
        float(np.clip(high_px / denominator, 0.0, 1.0)),
    )


def detect_gauge(
    frame: np.ndarray,
    button: Box | None,
    config: DetectorConfig,
) -> tuple[Box | None, float, float | None, float | None, tuple[float, float] | None]:
    if button is None:
        return None, 0.0, None, None, None
    height, width = frame.shape[:2]
    radius = max(4.0, min(button.w, button.h) / 2.0)
    roi = _relative_roi(
        button.cx,
        button.y - radius * 5.8,
        radius * 4.6,
        button.y - radius * 0.10,
        width,
        height,
    )
    crop = _crop(frame, roi)
    yellow = color_mask(crop, "yellow")
    blue = color_mask(crop, "blue")
    red = color_mask(crop, "red")
    colour = cv2.bitwise_or(yellow, blue)
    colour = cv2.bitwise_or(colour, red)

    # The bar has a distinctive dark, rounded body.  Colour-only detection
    # sees only the yellow segment and therefore mistakes the yellow progress
    # strip inside the purple prompt for the QTE bar.  Candidate bodies are
    # constrained relative to the detected action button and must also contain
    # enough coloured pixels.
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    dark = cv2.inRange(gray, 0, 105)
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((7, 17), np.uint8))
    candidates: list[tuple[float, Box]] = []
    for contour in _contours(dark):
        x, y, w, h = cv2.boundingRect(contour)
        if h <= 0:
            continue
        aspect = w / float(h)
        if aspect < config.gauge_min_aspect or w < radius * config.gauge_min_width_button_radius:
            continue
        absolute = Box(roi.x + x, roi.y + y, w, h)
        if abs(absolute.cx - button.cx) > radius * 2.0:
            continue
        colour_pixels = int(cv2.countNonZero(colour[y : y + h, x : x + w]))
        if colour_pixels < config.gauge_min_color_pixels:
            continue
        density = min(1.0, colour_pixels / max(1.0, w * h * 0.18))
        shape_score = min(1.0, (aspect - config.gauge_min_aspect) / 7.0 + 0.35)
        center_score = 1.0 - min(1.0, abs(absolute.cx - button.cx) / max(1.0, radius * 2.0))
        candidates.append((float(0.42 * shape_score + 0.35 * density + 0.23 * center_score), absolute))
    # Bright segments can hide portions of the dark body during the animated
    # quality flash.  Keep a colour-span fallback for those frames; prompt
    # progress bars are removed later when they overlap the prompt box.
    colour_box, colour_score = _find_horizontal_colour_region(
        colour,
        roi,
        config.gauge_min_aspect,
        radius * config.gauge_min_width_button_radius,
        config.gauge_min_color_pixels,
    )
    if colour_box is not None and abs(colour_box.cx - button.cx) <= radius * 2.0:
        candidates.append((min(0.82, float(colour_score)), colour_box))
    if not candidates:
        return None, 0.0, None, None, None
    score, box = max(candidates, key=lambda item: item[0])
    local = Box(box.x - roi.x, box.y - roi.y, box.w, box.h)
    red_region = red[local.y : local.bottom, local.x : local.right]
    marker_x: float | None = None
    marker_width: float | None = None
    red_points = cv2.findNonZero(red_region)
    if red_points is not None and len(red_points) >= 3:
        red_coords = red_points.reshape(-1, 2)
        marker_x = float(np.median(red_coords[:, 0]) / max(1, box.w - 1))
        marker_x = float(np.clip(marker_x, 0.0, 1.0))
        low_x, high_x = np.quantile(red_coords[:, 0], (0.05, 0.95))
        marker_span_px = int(math.ceil(float(high_x)) - math.floor(float(low_x)) + 1)
        candidate_width = marker_span_px / max(1, box.w)
        if candidate_width <= max(0.0, config.gauge_marker_max_width_ratio):
            marker_width = float(np.clip(candidate_width, 0.0, 1.0))

    yellow_region = yellow[local.y : local.bottom, local.x : local.right]
    target_range = _target_range_from_yellow(yellow_region, box.w, config)
    return box, score, marker_x, marker_width, target_range


def refine_gauge_roi(
    frame: np.ndarray,
    gauge: Box,
    config: DetectorConfig,
) -> _GaugeRefinement:
    """Refine marker and target geometry from a padded full-resolution ROI.

    Gauge localisation remains a work-frame operation.  This helper deliberately
    receives the already mapped gauge box and never searches the full frame, so
    high-resolution colour analysis is limited to the small region where the
    moving QTE elements are expected.
    """

    height, width = frame.shape[:2]
    if gauge.w <= 1 or gauge.h <= 0 or width <= 0 or height <= 0:
        return _GaugeRefinement(None, None, None, 0, 0)
    padding = max(0.0, float(config.gauge_full_res_refine_padding_ratio))
    pad_x = max(1, int(round(gauge.w * padding)))
    pad_y = max(1, int(round(gauge.h * padding)))
    roi = _clip_box(
        gauge.x - pad_x,
        gauge.y - pad_y,
        gauge.w + 2 * pad_x,
        gauge.h + 2 * pad_y,
        width,
        height,
    )
    local_gauge = _clip_box(
        gauge.x - roi.x,
        gauge.y - roi.y,
        gauge.w,
        gauge.h,
        roi.w,
        roi.h,
    )
    crop = _crop(_crop(frame, roi), local_gauge)
    if crop.size == 0 or local_gauge.w <= 1:
        return _GaugeRefinement(None, None, None, 0, 0)

    red = color_mask(crop, "red")
    yellow = color_mask(crop, "yellow")
    red_points = cv2.findNonZero(red)
    marker_x: float | None = None
    marker_width: float | None = None
    marker_pixels = int(cv2.countNonZero(red))
    if red_points is not None and len(red_points) >= 3:
        red_coords = red_points.reshape(-1, 2)
        marker_x = float(np.median(red_coords[:, 0]) / max(1, local_gauge.w - 1))
        marker_x = float(np.clip(marker_x, 0.0, 1.0))
        low_x, high_x = np.quantile(red_coords[:, 0], (0.05, 0.95))
        marker_span_px = int(math.ceil(float(high_x)) - math.floor(float(low_x)) + 1)
        candidate_width = marker_span_px / max(1, local_gauge.w)
        if candidate_width <= max(0.0, config.gauge_marker_max_width_ratio):
            marker_width = float(np.clip(candidate_width, 0.0, 1.0))

    target_pixels = int(cv2.countNonZero(yellow))
    target_range = _target_range_from_yellow(yellow, local_gauge.w, config)
    return _GaugeRefinement(marker_x, marker_width, target_range, marker_pixels, target_pixels)


def detect_quality(
    frame: np.ndarray,
    button: Box | None,
    gauge: Box | None,
    config: DetectorConfig,
) -> tuple[str | None, float, dict[str, int]]:
    if button is None or gauge is None:
        return None, 0.0, {}
    height, width = frame.shape[:2]
    radius = max(4.0, min(button.w, button.h) / 2.0)
    roi_top = gauge.y - radius * 3.10
    roi_bottom = gauge.y - radius * 0.22
    roi = _clip_box(gauge.x - radius * 0.9, roi_top, gauge.w + radius * 1.8, roi_bottom - roi_top, width, height)
    crop = _crop(frame, roi)
    # Water is broad and low-frequency; opening removes much of it while
    # leaving the saturated, high-contrast quality word.
    masks = {
        "cool": color_mask(crop, "blue"),
        "great": color_mask(crop, "pink"),
        "perfect": color_mask(crop, "orange"),
    }
    scores: dict[str, tuple[float, int]] = {}
    pixel_counts: dict[str, int] = {}
    for name, mask in masks.items():
        cleaned = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        pixel_count, largest = _compact_component_pixels(cleaned)
        pixel_counts[name] = pixel_count
        density = pixel_count / max(1.0, roi.w * roi.h)
        compact = min(1.0, largest / max(1.0, roi.w * roi.h * 0.16))
        score = min(1.0, pixel_count / max(1.0, config.quality_min_pixels * 3.5)) * 0.70 + min(1.0, density / 0.035) * 0.30
        score *= 0.72 + 0.28 * compact
        scores[name] = (float(score), pixel_count)
    best_name, (best_score, best_pixels) = max(scores.items(), key=lambda item: item[1][0])
    if best_pixels < config.quality_min_pixels or best_score < 0.22:
        return None, 0.0, pixel_counts
    return best_name, float(np.clip(best_score, 0.0, 1.0)), pixel_counts


def detect_continue_button(frame: np.ndarray, result_visible: bool) -> Box | None:
    if not result_visible:
        return None
    height, width = frame.shape[:2]
    roi = _clip_box(0, height * 0.52, width, height * 0.45, width, height)
    crop = _crop(frame, roi)
    # Keep this opt-in path conservative.  The result screen contains many
    # yellow reward/weight elements, so a yellow-only match could tap a
    # reward label.  A continue control must be a wide, compact green
    # control; callers still need to explicitly enable auto-continue.
    green = color_mask(crop, "green")
    combined = cv2.morphologyEx(green, cv2.MORPH_CLOSE, np.ones((9, 15), np.uint8))
    candidates: list[tuple[float, Box]] = []
    for contour in _contours(combined):
        x, y, w, h = cv2.boundingRect(contour)
        if h <= 0 or w / float(h) < 2.0 or w < width * 0.18:
            continue
        green_ratio = cv2.countNonZero(green[y : y + h, x : x + w]) / max(1.0, w * h)
        if green_ratio < 0.20:
            continue
        fill = cv2.contourArea(contour) / max(1.0, w * h)
        box = Box(roi.x + x, roi.y + y, w, h)
        candidates.append((float(np.clip(0.65 * fill + 0.35 * green_ratio, 0.0, 1.0)), box))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]

    # Some result variants use a small dismiss/continue X instead of a green
    # bar. The icon can be near-black in a dimmed result frame or bright white
    # during the reward animation, so inspect several luminance bands. Every
    # candidate is still constrained by the current framebuffer's relative
    # bottom-center geometry; this is not a fixed coordinate fallback.
    icon_roi = _clip_box(width * 0.30, height * 0.92, width * 0.40, height * 0.075, width, height)
    icon_crop = _crop(frame, icon_roi)
    gray = cv2.cvtColor(icon_crop, cv2.COLOR_BGR2GRAY)
    icon_candidates: list[tuple[float, Box]] = []
    min_area = max(40, int(width * height * 0.00005))
    for low, high in ((5, 80), (8, 120), (20, 130), (130, 255), (180, 255)):
        icon_mask = cv2.inRange(gray, low, high)
        icon_mask = cv2.morphologyEx(icon_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        icon_mask = cv2.morphologyEx(icon_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        for contour in _contours(icon_mask):
            x, y, w, h = cv2.boundingRect(contour)
            area = cv2.contourArea(contour)
            if (
                area < min_area
                or w <= 0
                or h <= 0
                or not 0.35 <= w / float(h) <= 2.8
                or w > width * 0.18
                or h > height * 0.10
            ):
                continue
            box = Box(icon_roi.x + x, icon_roi.y + y, w, h)
            if box.cy < height * 0.94 or abs(box.cx - width / 2.0) > width * 0.12:
                continue
            fill = area / max(1.0, w * h)
            density = cv2.countNonZero(icon_mask[y : y + h, x : x + w]) / max(1.0, w * h)
            center_score = 1.0 - min(1.0, abs(box.cx - width / 2.0) / max(1.0, width * 0.12))
            vertical_score = 1.0 - min(1.0, abs(box.cy - height * 0.965) / max(1.0, height * 0.035))
            score = 0.45 * center_score + 0.25 * vertical_score + 0.30 * min(1.0, max(fill, density) / 0.35)
            icon_candidates.append((float(np.clip(score, 0.0, 1.0)), box))
    return max(icon_candidates, key=lambda item: item[0])[1] if icon_candidates else None


def detect_result_fallback_tap(
    frame: np.ndarray,
    result_visible: bool,
    continue_box: Box | None = None,
) -> Box | None:
    """Find one safe, dynamic tap target inside a result reward animation.

    A few result variants keep the reward animation on screen after the
    visible dismiss/continue control is tapped.  The fallback is intentionally
    limited to the central reward content: it uses the largest compact,
    high-contrast contour in a ratio-based ROI and never falls back to a fixed
    screen coordinate.  If no convincing content contour exists, returning
    ``None`` lets the live runner fail safe instead of guessing.
    """

    if not result_visible:
        return None
    height, width = frame.shape[:2]
    roi = _clip_box(width * 0.12, height * 0.16, width * 0.76, height * 0.66, width, height)
    crop = _crop(frame, roi)
    if crop.size == 0:
        return None

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 30, 120)
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))

    component_count, _, stats, centroids = cv2.connectedComponentsWithStats(edges, 8)
    frame_area = float(max(1, width * height))
    min_area = max(120, int(frame_area * 0.002))
    candidates: list[tuple[float, Box]] = []
    for index in range(1, component_count):
        x, y, box_width, box_height, area = [int(value) for value in stats[index]]
        if area < min_area or box_width <= 0 or box_height <= 0:
            continue
        # Border-touching components are usually the dimmed game background,
        # dock, or celebration bands rather than the reward object.
        if x <= 0 or y <= 0 or x + box_width >= roi.w - 1 or y + box_height >= roi.h - 1:
            continue
        box = Box(roi.x + x, roi.y + y, box_width, box_height)
        if continue_box is not None and _overlap_ratio(box, continue_box) >= 0.20:
            continue
        center_x = float(centroids[index][0] + roi.x)
        center_y = float(centroids[index][1] + roi.y)
        if abs(center_x - width / 2.0) > width * 0.34:
            continue
        compactness = float(area / max(1.0, box_width * box_height))
        area_score = min(1.0, area / max(1.0, frame_area * 0.025))
        center_score = 1.0 - min(1.0, abs(center_x - width / 2.0) / max(1.0, width * 0.34))
        vertical_score = 1.0 - min(1.0, abs(center_y - height * 0.48) / max(1.0, height * 0.32))
        compact_score = min(1.0, compactness / 0.40)
        score = float(
            0.38 * area_score
            + 0.24 * center_score
            + 0.20 * vertical_score
            + 0.18 * compact_score
        )
        candidates.append((score, box))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


class FrameAnalyzer:
    """Visual observations with bounded temporal continuity for moving UI."""

    def __init__(self, config: DetectorConfig):
        self.config = config
        self.previous_gray: np.ndarray | None = None
        self.previous_button_work: Box | None = None
        self.previous_work_size: tuple[int, int] | None = None
        self.previous_gauge_work: Box | None = None
        self.target_range_history: deque[tuple[float, float]] = deque(
            maxlen=max(1, int(config.gauge_target_tracking_frames))
        )
        self.target_range_missing_frames = 0
        self.tracked_target_range: tuple[float, float] | None = None
        self.target_tracking_mode = "reset"
        self.target_marker_occluded = False

    @staticmethod
    def _gauge_is_continuous(previous: Box | None, current: Box) -> bool:
        if previous is None:
            return False
        width = max(1.0, float(previous.w), float(current.w))
        height = max(1.0, float(previous.h), float(current.h))
        return (
            abs(previous.cx - current.cx) <= width * 0.14
            and abs(previous.cy - current.cy) <= height * 0.80
            and abs(previous.w - current.w) <= width * 0.18
            and abs(previous.h - current.h) <= height * 0.55
        )

    @staticmethod
    def _target_envelope(ranges: Iterable[tuple[float, float]]) -> tuple[float, float] | None:
        values = list(ranges)
        if not values:
            return None
        return min(item[0] for item in values), max(item[1] for item in values)

    def _stabilize_target_range(
        self,
        gauge: Box | None,
        target_range: tuple[float, float] | None,
        marker_width: float | None,
        marker_x: float | None = None,
    ) -> tuple[float, float] | None:
        """Track a target with immediate contraction and guarded recovery.

        A yellow span can become wider when the marker or a compression
        artefact hides one of its edges.  The old tracker unioned every valid
        frame, which made a stale wide envelope survive a real target shrink.
        This tracker therefore accepts a narrower raw span immediately.  It
        only expands after repeated compatible evidence, and a missing target
        reuses the latest tracked span for a bounded number of frames.
        """

        if gauge is None:
            self.previous_gauge_work = None
            self.target_range_history.clear()
            self.target_range_missing_frames = 0
            self.tracked_target_range = None
            self.target_tracking_mode = "reset_no_gauge"
            self.target_marker_occluded = False
            return None

        if not self._gauge_is_continuous(self.previous_gauge_work, gauge):
            self.target_range_history.clear()
            self.target_range_missing_frames = 0
            self.tracked_target_range = None
            self.target_tracking_mode = "reset_gauge_discontinuity"
        self.previous_gauge_work = gauge

        if target_range is None:
            self.target_range_missing_frames += 1
            marker_occluded = False
            if self.tracked_target_range is not None and marker_x is not None:
                target_center = sum(self.tracked_target_range) / 2.0
                target_half_width = (self.tracked_target_range[1] - self.tracked_target_range[0]) / 2.0
                marker_half_width = max(0.0, float(marker_width or 0.0)) / 2.0
                marker_occluded = abs(float(marker_x) - target_center) <= target_half_width + marker_half_width
            self.target_marker_occluded = marker_occluded
            if (
                self.tracked_target_range is not None
                and self.target_range_missing_frames
                <= max(0, int(self.config.gauge_target_tracking_missing_frames))
            ):
                self.target_tracking_mode = "recovery_marker_occlusion" if marker_occluded else "recovery_missing"
                return self.tracked_target_range
            self.target_range_history.clear()
            self.tracked_target_range = None
            self.target_tracking_mode = "reset_target_missing"
            return None

        self.target_range_missing_frames = 0
        self.target_marker_occluded = False
        low, high = sorted((float(target_range[0]), float(target_range[1])))
        current = (float(np.clip(low, 0.0, 1.0)), float(np.clip(high, 0.0, 1.0)))
        if current[1] <= current[0]:
            self.target_range_history.clear()
            self.tracked_target_range = None
            self.target_tracking_mode = "reset_invalid_target"
            return None

        previous = self.tracked_target_range
        if previous is None:
            self.target_range_history.append(current)
            self.tracked_target_range = current
            self.target_tracking_mode = "raw_initial"
            return current

        previous_width = previous[1] - previous[0]
        current_width = current[1] - current[0]
        union = (min(previous[0], current[0]), max(previous[1], current[1]))
        marker_gap = max(0.0, float(marker_width or 0.0)) * 0.75
        allowed_gap = max(float(self.config.gauge_target_tracking_max_gap_ratio), marker_gap)
        allowed_width = max(0.0, float(self.config.gauge_target_tracking_max_width_ratio))
        overlap = min(previous[1], current[1]) - max(previous[0], current[0])
        gap = max(0.0, max(previous[0], current[0]) - min(previous[1], current[1]))

        # Contraction is the important safety path: replace the old span
        # straight away and discard older envelope samples.
        if current_width < previous_width - 1e-6:
            self.target_range_history.clear()
            self.target_range_history.append(current)
            self.tracked_target_range = current
            self.target_tracking_mode = "raw_shrink"
            return current

        # A wider observation that still overlaps the previous target may be
        # a genuine re-expansion or a stale occluded span.  In either case it
        # must earn its way back into the click range over several frames;
        # only a clearly disjoint target is allowed to reset immediately.
        if current_width > previous_width + 1e-6:
            if overlap < 0.0 and gap > allowed_gap:
                self.target_range_history.clear()
                self.target_range_history.append(current)
                self.tracked_target_range = current
                self.target_tracking_mode = "raw_reset_jump"
                return current
            self.target_range_history.append(current)
            required = max(1, int(self.config.gauge_target_tracking_frames))
            if len(self.target_range_history) < required:
                self.target_tracking_mode = "hold_expansion"
                return previous
            envelope = self._target_envelope(self.target_range_history) or current
            if envelope[1] - envelope[0] > allowed_width + 1e-6:
                # Never let repeated wide observations enlarge the safe click
                # range beyond the configured bound after a shrink.
                self.target_range_history.clear()
                self.target_range_history.append(previous)
                self.target_tracking_mode = "hold_expansion_over_width"
                return previous
            self.tracked_target_range = envelope
            self.target_tracking_mode = "recovered_expansion"
            return envelope

        compatible = (
            union[1] - union[0] <= allowed_width + 1e-6
            and (overlap >= 0.0 or gap <= allowed_gap)
        )
        if not compatible:
            self.target_range_history.clear()
            self.target_range_history.append(current)
            self.tracked_target_range = current
            self.target_tracking_mode = "raw_reset_jump"
            return current

        self.target_range_history.append(current)
        self.tracked_target_range = current
        self.target_tracking_mode = "raw_stable"
        return current

    def _water_activity(self, gray: np.ndarray, button: Box | None) -> float:
        small = cv2.resize(gray, (160, max(80, int(round(gray.shape[0] * 160 / gray.shape[1])))), interpolation=cv2.INTER_AREA)
        if self.previous_gray is None or self.previous_gray.shape != small.shape:
            self.previous_gray = small
            return 0.0
        diff = cv2.absdiff(small, self.previous_gray)
        self.previous_gray = small
        height, width = diff.shape[:2]
        # The ratio-based water band excludes the top HUD and the bottom dock.
        x0, x1 = int(width * 0.12), int(width * 0.88)
        y0 = int(height * 0.22)
        y1 = int(height * (0.78 if button is None else np.clip(button.y / max(1, gray.shape[0]), 0.55, 0.86)))
        roi = diff[y0:max(y0 + 1, y1), x0:x1]
        if roi.size == 0:
            return 0.0
        return float(np.mean(roi >= self.config.motion_threshold))

    def analyze(self, frame: np.ndarray, frame_index: int, timestamp_s: float, *, fast: bool = False) -> Detection:
        height, width = frame.shape[:2]
        work, scale = _prepare_frame(frame, self.config.max_work_width)
        work_h, work_w = work.shape[:2]
        gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
        if fast and self.previous_button_work is not None and self.previous_work_size == (work_w, work_h):
            work_button, active, button_score, button_features = detect_action_button_nearby(
                work,
                self.previous_button_work,
                self.config,
            )
            if work_button is None:
                work_button, active, button_score, button_features = detect_action_button(work, self.config)
        else:
            work_button, active, button_score, button_features = detect_action_button(work, self.config)
        self.previous_button_work = work_button
        self.previous_work_size = (work_w, work_h)
        work_prompt, prompt_score = detect_prompt(work, work_button, self.config)
        work_gauge, gauge_score, marker_x, marker_width, target_range = detect_gauge(work, work_button, self.config)
        prompt_progress = _overlap_ratio(work_prompt, work_gauge) >= 0.30
        if prompt_progress:
            # The prompt itself contains a yellow progress strip.  It is not
            # the later QTE bar and must not trigger QTE/quality states.
            work_gauge, gauge_score, marker_x, marker_width, target_range = None, 0.0, None, None, None
        work_quality, quality_score, quality_pixels = detect_quality(work, work_button, work_gauge, self.config)
        gauge_state_visible = work_gauge is not None and gauge_score >= self.config.gauge_min_state_score
        quality_state_visible = work_quality is not None and gauge_state_visible

        button = _restore_box(work_button, scale, width, height)
        prompt = _restore_box(work_prompt, scale, width, height)
        gauge = _restore_box(work_gauge, scale, width, height)
        raw_target_range = target_range
        marker_source = "work" if marker_x is not None else "none"
        marker_width_source = "work" if marker_width is not None else "none"
        target_source = "work" if raw_target_range is not None else "none"
        refinement = _GaugeRefinement(None, None, None, 0, 0)
        if gauge is not None and self.config.gauge_full_res_refine_enabled:
            refinement = refine_gauge_roi(frame, gauge, self.config)
            if refinement.marker_x is not None:
                marker_x = refinement.marker_x
                marker_source = "full_res_roi"
            if refinement.marker_width is not None:
                marker_width = refinement.marker_width
                marker_width_source = "full_res_roi"
            if refinement.target_range is not None:
                raw_target_range = refinement.target_range
                target_source = "full_res_roi"
        tracked_target_range = self._stabilize_target_range(
            work_gauge,
            raw_target_range,
            marker_width,
            marker_x,
        )
        # Result detection runs on the original frame scale only through
        # ratios, so the luma calculation remains comparable across devices.
        original_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        luma = float(np.mean(original_gray))
        center = _clip_box(width * 0.12, height * 0.12, width * 0.76, height * 0.76, width, height)
        center_yellow = color_mask(_crop(frame, center), "yellow")
        yellow_pixels = int(cv2.countNonZero(center_yellow))
        dark_ratio = float(np.mean(original_gray < 82))
        yellow_ratio = float(yellow_pixels / max(1, center.area))
        result_score = float(np.clip(
            0.55 * _ratio(self.config.result_dark_luma - luma, 0.0, self.config.result_dark_luma * 0.52)
            + 0.45 * _ratio(
                yellow_ratio,
                self.config.result_yellow_ratio * 0.45,
                self.config.result_yellow_ratio * 2.0,
            ),
            0.0,
            1.0,
        ))
        result_visible = (luma <= self.config.result_dark_luma and yellow_ratio >= self.config.result_yellow_ratio) or (
            dark_ratio >= 0.48 and yellow_ratio >= self.config.result_yellow_ratio * 0.7
        )
        if result_visible:
            result_score = max(result_score, 0.72)
        result_box = Box(0, 0, width, height) if result_visible else None
        continue_box = detect_continue_button(frame, result_visible)
        result_fallback_box = detect_result_fallback_tap(frame, result_visible, continue_box)
        water_activity = self._water_activity(gray, work_button)

        if result_visible:
            hint = FishingState.RESULT
            confidence = result_score
        elif quality_state_visible:
            hint = FishingState.QUALITY
            confidence = quality_score
        elif gauge_state_visible:
            hint = FishingState.QTE
            confidence = gauge_score
        elif prompt is not None:
            hint = FishingState.PROMPT
            confidence = prompt_score
        elif button is not None and active and water_activity >= self.config.motion_min_ratio:
            hint = FishingState.CASTING
            confidence = min(0.96, 0.45 + water_activity * 7.0 + button_score * 0.25)
        elif button is not None:
            hint = FishingState.WAITING
            confidence = max(0.40, button_score)
        else:
            hint = FishingState.UNKNOWN
            confidence = 0.0

        features: dict[str, object] = {
            "work_scale": round(scale, 5),
            "luma": round(luma, 2),
            "dark_ratio": round(dark_ratio, 4),
            "center_yellow_pixels": yellow_pixels,
            "center_yellow_ratio": round(yellow_ratio, 6),
            "quality_pixels": quality_pixels,
            "prompt_progress": prompt_progress,
            "gauge_marker_width": round(marker_width, 5) if marker_width is not None else None,
            "gauge_target_width": round(tracked_target_range[1] - tracked_target_range[0], 5) if tracked_target_range else None,
            "gauge_target_track_samples": len(self.target_range_history),
            "gauge_target_tracking_mode": self.target_tracking_mode,
            "gauge_target_marker_occluded": self.target_marker_occluded,
            "gauge_target_missing_frames": self.target_range_missing_frames,
            "gauge_raw_target_range": list(raw_target_range) if raw_target_range else None,
            "gauge_tracked_target_range": list(tracked_target_range) if tracked_target_range else None,
            "gauge_safe_click_range": list(tracked_target_range) if tracked_target_range else None,
            "gauge_refine": {
                "enabled": bool(self.config.gauge_full_res_refine_enabled),
                "attempted": gauge is not None and bool(self.config.gauge_full_res_refine_enabled),
                "marker_source": marker_source,
                "marker_width_source": marker_width_source,
                "target_source": target_source,
                "marker_pixels": refinement.marker_pixels,
                "target_pixels": refinement.target_pixels,
            },
            **button_features,
        }
        return Detection(
            frame_index=frame_index,
            timestamp_s=timestamp_s,
            frame_width=width,
            frame_height=height,
            action_button=button,
            action_active=active,
            action_score=button_score,
            prompt_box=prompt,
            prompt_score=prompt_score,
            gauge_box=gauge,
            gauge_score=gauge_score,
            gauge_marker_x=marker_x,
            gauge_marker_width=marker_width,
            gauge_target_range=tracked_target_range,
            gauge_raw_target_range=raw_target_range,
            gauge_tracked_target_range=tracked_target_range,
            gauge_safe_click_range=tracked_target_range,
            quality=work_quality,
            quality_score=quality_score,
            result_box=result_box,
            result_score=result_score,
            continue_box=continue_box,
            result_fallback_box=result_fallback_box,
            water_activity=water_activity,
            hint=hint,
            confidence=float(np.clip(confidence, 0.0, 1.0)),
            features=features,
        )
