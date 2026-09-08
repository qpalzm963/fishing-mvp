from pathlib import Path

import cv2
import numpy as np

from fishing_mvp.config import DetectorConfig
from fishing_mvp.models import Box, FishingState
from fishing_mvp.vision import (
    FrameAnalyzer,
    _target_range_from_yellow,
    color_mask,
    detect_continue_button,
    detect_result_fallback_tap,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> np.ndarray:
    frame = cv2.imread(str(FIXTURE_DIR / f"{name}.jpg"), cv2.IMREAD_COLOR)
    assert frame is not None, f"missing fixture: {name}"
    return frame


def test_waiting_fixture_detects_inactive_action_control():
    detection = FrameAnalyzer(DetectorConfig()).analyze(load_fixture("waiting"), 0, 0.0)
    assert detection.hint == FishingState.WAITING
    assert detection.action_button is not None
    assert not detection.action_active
    assert detection.prompt_box is None
    assert detection.gauge_box is None
    assert detection.result_box is None


def test_prompt_fixture_detects_relative_action_and_banner():
    detection = FrameAnalyzer(DetectorConfig()).analyze(load_fixture("prompt"), 0, 0.0)
    assert detection.hint == FishingState.PROMPT
    assert detection.action_button is not None and detection.action_active
    assert detection.prompt_box is not None
    assert detection.gauge_box is None
    assert abs(detection.action_button.cx / detection.frame_width - 0.5) < 0.12
    assert 0.70 < detection.action_button.cy / detection.frame_height < 0.95


def test_qte_fixture_detects_gauge_marker_and_target_range():
    detection = FrameAnalyzer(DetectorConfig()).analyze(load_fixture("qte"), 0, 0.0)
    assert detection.hint == FishingState.QTE
    assert detection.gauge_box is not None
    assert detection.gauge_marker_x is not None
    assert detection.gauge_target_range is not None
    low, high = detection.gauge_target_range
    assert 0.0 <= low < high <= 1.0
    assert 0.0 <= detection.gauge_marker_x <= 1.0
    assert detection.gauge_marker_width is not None
    assert detection.features["gauge_target_width"] == round(high - low, 5)


def test_narrow_yellow_target_is_accepted_but_sparse_noise_is_rejected():
    config = DetectorConfig()
    narrow = np.zeros((12, 120), dtype=np.uint8)
    narrow[:, 54:62] = 255
    target = _target_range_from_yellow(narrow, 120, config)

    assert target is not None
    assert target[1] - target[0] < 0.10

    sparse = np.zeros((12, 120), dtype=np.uint8)
    for x in (20, 40, 60, 80):
        sparse[:, x] = 255
    assert _target_range_from_yellow(sparse, 120, config) is None


def test_narrow_target_continuity_bridges_marker_occlusion_without_stale_carryover():
    config = DetectorConfig(
        gauge_target_tracking_frames=4,
        gauge_target_tracking_max_width_ratio=0.18,
        gauge_target_tracking_max_gap_ratio=0.06,
    )
    analyzer = FrameAnalyzer(config)
    gauge = Box(100, 500, 300, 40)

    assert analyzer._stabilize_target_range(gauge, (0.650, 0.719), 0.08) == (0.650, 0.719)
    analyzer._stabilize_target_range(gauge, (0.688, 0.719), 0.08)
    bridged = analyzer._stabilize_target_range(gauge, (0.623, 0.657), 0.08)
    assert bridged is not None
    assert bridged[0] == 0.623
    assert bridged[1] == 0.719

    reset = analyzer._stabilize_target_range(gauge, (0.120, 0.180), 0.08)
    assert reset == (0.120, 0.180)


def test_qte_fast_path_keeps_dynamic_button_and_gauge_detection():
    analyzer = FrameAnalyzer(DetectorConfig())
    frame = load_fixture("qte")
    analyzer.analyze(frame, 0, 0.0)
    detection = analyzer.analyze(frame, 1, 1 / 30.0, fast=True)

    assert detection.hint == FishingState.QTE
    assert detection.action_button is not None
    assert detection.gauge_box is not None
    assert detection.gauge_marker_x is not None


def test_quality_fixture_detects_quality_label_without_ocr():
    detection = FrameAnalyzer(DetectorConfig()).analyze(load_fixture("quality"), 0, 0.0)
    assert detection.hint == FishingState.QUALITY
    assert detection.quality in {"cool", "great", "perfect"}
    assert detection.gauge_box is not None


def test_result_fixture_uses_normalized_yellow_ratio_and_detects_bottom_dismiss_icon():
    frame = load_fixture("result")
    config = DetectorConfig()
    detection = FrameAnalyzer(config).analyze(frame, 0, 0.0)
    assert detection.hint == FishingState.RESULT
    assert detection.result_box is not None
    assert detection.features["center_yellow_ratio"] > config.result_yellow_ratio
    assert detection.continue_box is not None
    assert abs(detection.continue_box.cx / detection.frame_width - 0.5) < 0.1
    assert detection.continue_box.cy / detection.frame_height > 0.9
    assert detection.result_fallback_box is not None

    smaller = cv2.resize(frame, (270, 585), interpolation=cv2.INTER_AREA)
    smaller_detection = FrameAnalyzer(config).analyze(smaller, 0, 0.0)
    assert smaller_detection.result_box is not None
    assert smaller_detection.features["center_yellow_ratio"] > config.result_yellow_ratio


def test_continue_detector_accepts_a_wide_green_control():
    frame = np.zeros((1170, 540, 3), dtype=np.uint8)
    cv2.rectangle(frame, (120, 800), (420, 900), (0, 220, 0), -1)
    box = detect_continue_button(frame, result_visible=True)
    assert box is not None
    assert box.w > box.h * 2


def test_continue_detector_accepts_a_centered_gray_dismiss_icon():
    frame = np.zeros((1170, 540, 3), dtype=np.uint8)
    cv2.line(frame, (250, 1100), (290, 1140), (70, 70, 70), 8)
    cv2.line(frame, (290, 1100), (250, 1140), (70, 70, 70), 8)
    box = detect_continue_button(frame, result_visible=True)
    assert box is not None
    assert abs(box.cx / frame.shape[1] - 0.5) < 0.1
    assert box.y / frame.shape[0] > 0.9


def test_result_fallback_detector_uses_a_central_reward_contour():
    frame = load_fixture("result")
    box = detect_result_fallback_tap(frame, result_visible=True)
    assert box is not None
    assert abs(box.cx / frame.shape[1] - 0.5) < 0.2
    assert 0.25 < box.cy / frame.shape[0] < 0.70


def test_purple_mask_uses_detector_hsv_configuration():
    hsv_pixel = np.uint8([[[140, 220, 220]]])
    bgr_pixel = cv2.cvtColor(hsv_pixel, cv2.COLOR_HSV2BGR)[0, 0].tolist()
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    image[20:60, 30:90] = bgr_pixel

    default_count = cv2.countNonZero(color_mask(image, "purple"))
    restricted = DetectorConfig(purple_hue_low=150, purple_hue_high=179)
    restricted_count = cv2.countNonZero(color_mask(image, "purple", config=restricted))
    assert default_count > 0
    assert restricted_count == 0
