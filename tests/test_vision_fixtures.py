from pathlib import Path

import cv2
import numpy as np

from fishing_mvp.config import DetectorConfig
from fishing_mvp.models import FishingState
from fishing_mvp.vision import FrameAnalyzer, color_mask, detect_continue_button


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


def test_quality_fixture_detects_quality_label_without_ocr():
    detection = FrameAnalyzer(DetectorConfig()).analyze(load_fixture("quality"), 0, 0.0)
    assert detection.hint == FishingState.QUALITY
    assert detection.quality in {"cool", "great", "perfect"}
    assert detection.gauge_box is not None


def test_result_fixture_uses_normalized_yellow_ratio_and_no_false_continue():
    frame = load_fixture("result")
    config = DetectorConfig()
    detection = FrameAnalyzer(config).analyze(frame, 0, 0.0)
    assert detection.hint == FishingState.RESULT
    assert detection.result_box is not None
    assert detection.features["center_yellow_ratio"] > config.result_yellow_ratio
    assert detection.continue_box is None

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
