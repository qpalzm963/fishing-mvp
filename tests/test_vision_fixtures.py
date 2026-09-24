from pathlib import Path

import cv2
import numpy as np
import pytest

import fishing_mvp.vision as vision
from fishing_mvp.config import DetectorConfig
from fishing_mvp.models import Box, FishingState
from fishing_mvp.vision import (
    FrameAnalyzer,
    _target_range_from_yellow,
    color_mask,
    detect_continue_button,
    detect_quality,
    detect_result_fallback_tap,
    refine_gauge_roi,
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
    refine = detection.features["gauge_refine"]
    assert refine["attempted"] is True
    assert refine["marker_source"] == "full_res_roi"
    assert refine["target_source"] == "full_res_roi"
    assert detection.gauge_raw_target_range == detection.gauge_tracked_target_range
    assert detection.gauge_safe_click_range == detection.gauge_target_range


def test_full_resolution_gauge_roi_refines_normalized_marker_and_target_without_full_frame_search():
    config = DetectorConfig()
    frame = np.zeros((220, 420, 3), dtype=np.uint8)
    gauge = Box(90, 100, 240, 24)
    yellow = cv2.cvtColor(np.uint8([[[28, 220, 240]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
    red = cv2.cvtColor(np.uint8([[[4, 230, 240]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
    frame[106:118, 150:182] = yellow
    frame[104:120, 246:255] = red

    refined = refine_gauge_roi(frame, gauge, config)

    assert refined.target_range is not None
    assert refined.marker_x is not None
    assert refined.marker_width is not None
    assert 0.23 < refined.target_range[0] < 0.30
    assert 0.35 < refined.target_range[1] < 0.40
    assert 0.63 < refined.marker_x < 0.72


def test_gauge_refine_can_be_disabled_without_changing_work_frame_detection():
    frame = load_fixture("qte")
    detection = FrameAnalyzer(DetectorConfig(gauge_full_res_refine_enabled=False)).analyze(frame, 0, 0.0)

    assert detection.gauge_box is not None
    assert detection.features["gauge_refine"]["attempted"] is False
    assert detection.features["gauge_refine"]["target_source"] == "work"
    assert detection.gauge_raw_target_range is not None


def test_full_resolution_refine_failure_falls_back_to_work_frame(monkeypatch):
    frame = load_fixture("qte")
    baseline = FrameAnalyzer(DetectorConfig(gauge_full_res_refine_enabled=False)).analyze(frame, 0, 0.0)
    monkeypatch.setattr(vision, "refine_gauge_roi", lambda *args: vision._GaugeRefinement(None, None, None, 0, 0))

    detection = FrameAnalyzer(DetectorConfig()).analyze(frame, 0, 0.0)

    assert detection.hint == FishingState.QTE
    assert detection.gauge_marker_x == baseline.gauge_marker_x
    assert detection.gauge_raw_target_range == baseline.gauge_raw_target_range
    assert detection.features["gauge_refine"]["marker_source"] == "work"
    assert detection.features["gauge_refine"]["target_source"] == "work"


def test_narrow_target_is_recovered_from_full_resolution_when_work_target_is_missing(monkeypatch):
    frame = np.zeros((2340, 1080, 3), dtype=np.uint8)
    yellow = cv2.cvtColor(np.uint8([[[28, 220, 240]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
    red = cv2.cvtColor(np.uint8([[[4, 230, 240]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
    frame[1660:1700, 530:548] = yellow
    frame[1650:1720, 650:660] = red
    monkeypatch.setattr(vision, "detect_action_button", lambda *args: (Box(191, 822, 98, 98), True, 0.9, {}))
    monkeypatch.setattr(vision, "detect_prompt", lambda *args: (None, 0.0))
    monkeypatch.setattr(vision, "detect_gauge", lambda *args: (Box(111, 729, 258, 58), 0.9, None, None, None))

    detection = FrameAnalyzer(DetectorConfig()).analyze(frame, 0, 0.0)

    assert detection.hint == FishingState.QTE
    assert detection.gauge_marker_x is not None
    assert detection.gauge_raw_target_range is not None
    assert detection.gauge_raw_target_range[1] - detection.gauge_raw_target_range[0] < 0.05
    assert detection.features["gauge_refine"]["target_source"] == "full_res_roi"


def test_empty_and_boundary_quality_roi_are_safe():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    diagnostics: dict[str, object] = {}
    result = detect_quality(frame, Box(40, 2, 8, 8), Box(35, 0, 18, 1), DetectorConfig(), diagnostics)
    assert result == (None, 0.0, {})
    assert diagnostics["status"] == "empty_roi"
    assert diagnostics["roi_box"]["h"] == 0

    diagnostics = {}
    result = detect_quality(frame, Box(40, 40, 20, 20), Box(30, 10, 40, 10), DetectorConfig(), diagnostics)
    assert result[0] is None
    assert diagnostics["status"] == "analyzed"
    assert diagnostics["roi_box"]["y"] == 0


def test_analyze_skips_empty_quality_roi_without_crashing(monkeypatch):
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    monkeypatch.setattr(vision, "detect_action_button", lambda *args: (Box(40, 2, 8, 8), True, 0.9, {}))
    monkeypatch.setattr(vision, "detect_prompt", lambda *args: (None, 0.0))
    monkeypatch.setattr(vision, "detect_gauge", lambda *args: (Box(35, 0, 18, 1), 0.9, 0.5, 0.1, (0.4, 0.6)))

    detection = FrameAnalyzer(DetectorConfig()).analyze(frame, 0, 0.0)

    assert detection.quality is None
    assert detection.features["quality_roi"]["status"] == "empty_roi"
    assert detection.features["gauge_geometry"]["status"] == "accepted"


def test_implausible_gauge_is_rejected_before_quality_analysis(monkeypatch):
    frame = load_fixture("qte_splash_16s")
    wrong_gauge = Box(100, 310, 260, 50)
    monkeypatch.setattr(vision, "detect_gauge", lambda *args: (wrong_gauge, 0.95, 0.5, 0.05, (0.4, 0.6)))

    detection = FrameAnalyzer(DetectorConfig()).analyze(frame, 0, 0.0)

    assert detection.gauge_box is None
    assert detection.quality is None
    assert detection.features["gauge_geometry"]["status"] == "implausible_gauge_position"
    assert detection.features["gauge_geometry"]["rejected_box_work"] == wrong_gauge.to_dict()
    assert detection.features["gauge_geometry"]["rejected_box_frame"]["y"] > wrong_gauge.y
    assert detection.features["quality_roi"]["status"] == "not_attempted"


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


def test_narrow_target_tracking_shrinks_immediately_and_recovers_boundedly():
    config = DetectorConfig(
        gauge_target_tracking_frames=4,
        gauge_target_tracking_max_width_ratio=0.18,
        gauge_target_tracking_max_gap_ratio=0.06,
    )
    analyzer = FrameAnalyzer(config)
    gauge = Box(100, 500, 300, 40)

    assert analyzer._stabilize_target_range(gauge, (0.620, 0.800), 0.08) == (0.620, 0.800)
    narrowed = analyzer._stabilize_target_range(gauge, (0.650, 0.700), 0.08)
    assert narrowed == (0.650, 0.700)

    recovered = analyzer._stabilize_target_range(gauge, None, 0.08, marker_x=0.675)
    assert recovered == narrowed
    assert analyzer.target_tracking_mode == "recovery_marker_occlusion"

    # A single wider observation cannot restore the old wide click range.
    held = analyzer._stabilize_target_range(gauge, (0.620, 0.800), 0.08)
    assert held == narrowed

    reset = analyzer._stabilize_target_range(gauge, (0.120, 0.180), 0.08)
    assert reset == (0.120, 0.180)


def test_narrow_target_tracking_resets_after_long_missing_or_gauge_jump():
    analyzer = FrameAnalyzer(DetectorConfig(gauge_target_tracking_missing_frames=2))
    gauge = Box(100, 500, 300, 40)
    assert analyzer._stabilize_target_range(gauge, (0.45, 0.55), 0.04) == (0.45, 0.55)
    assert analyzer._stabilize_target_range(gauge, None, 0.04) == (0.45, 0.55)
    assert analyzer._stabilize_target_range(gauge, None, 0.04) == (0.45, 0.55)
    assert analyzer._stabilize_target_range(gauge, None, 0.04) is None

    assert analyzer._stabilize_target_range(gauge, (0.45, 0.55), 0.04) == (0.45, 0.55)
    jumped_gauge = Box(500, 500, 300, 40)
    assert analyzer._stabilize_target_range(jumped_gauge, (0.70, 0.80), 0.04) == (0.70, 0.80)
    assert analyzer.target_tracking_mode == "raw_initial"


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


@pytest.mark.parametrize("name", ["qte_splash_16s", "qte_splash_20s", "qte_splash_21s"])
@pytest.mark.parametrize("width", [540, 1080])
def test_late_qte_water_splashes_do_not_suppress_input_as_cool(name, width):
    frame = cv2.resize(load_fixture(name), (width, width * 2340 // 1080))
    detection = FrameAnalyzer(DetectorConfig()).analyze(frame, 0, 0.0)
    assert detection.hint == FishingState.QTE
    assert detection.quality is None
    assert detection.gauge_marker_x is not None
    assert detection.gauge_target_range is not None


@pytest.mark.parametrize("width", [540, 1080])
def test_real_cool_letters_are_still_recognized(width):
    frame = cv2.resize(load_fixture("cool_20260921"), (width, width * 2340 // 1080))
    detection = FrameAnalyzer(DetectorConfig()).analyze(frame, 0, 0.0)
    assert detection.hint == FishingState.QUALITY
    assert detection.quality == "cool"


@pytest.mark.parametrize("visible,marker", [((0.50, 0.54), 0.46), ((0.44, 0.48), 0.52)])
def test_partial_marker_occlusion_keeps_target_center_with_bounded_recovery(visible, marker):
    analyzer = FrameAnalyzer(DetectorConfig(gauge_target_tracking_missing_frames=2))
    gauge = Box(100, 500, 300, 40)
    target = (0.44, 0.54)
    assert analyzer._stabilize_target_range(gauge, target, 0.08, 0.2) == target
    for _ in range(2):
        assert analyzer._stabilize_target_range(gauge, visible, 0.08, marker) == target
        assert analyzer.target_marker_occluded
    # Ambiguous evidence cannot retain an old target indefinitely.
    assert analyzer._stabilize_target_range(gauge, visible, 0.08, marker) == visible


def test_partial_occlusion_recovery_does_not_hide_real_shrink_or_target_move():
    analyzer = FrameAnalyzer(DetectorConfig())
    gauge = Box(100, 500, 300, 40)
    analyzer._stabilize_target_range(gauge, (0.44, 0.54), 0.08, 0.2)
    assert analyzer._stabilize_target_range(gauge, (0.50, 0.54), 0.08, 0.46) == (0.44, 0.54)
    assert analyzer._stabilize_target_range(gauge, (0.47, 0.51), 0.08, 0.2) == (0.47, 0.51)
    assert analyzer._stabilize_target_range(gauge, (0.64, 0.68), 0.08, 0.5) == (0.64, 0.68)


def test_recorded_marker_outline_does_not_shift_the_target_at_16_seconds():
    analyzer = FrameAnalyzer(DetectorConfig())
    gauge = Box(100, 500, 300, 40)
    target = (0.4386, 0.5273)
    analyzer._stabilize_target_range(gauge, target, 0.0767, 0.5973)
    # 16.775s and 16.808s: the white outline extends beyond the red core.
    assert analyzer._stabilize_target_range(gauge, (0.4352, 0.4795), 0.0767, 0.5358) == target
    assert analyzer._stabilize_target_range(gauge, (0.4334, 0.4642), 0.0767, 0.5154) == target
    assert analyzer._stabilize_target_range(gauge, None, 0.0767, 0.4966) == target


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
