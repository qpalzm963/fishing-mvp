import cv2
import numpy as np

from fishing_mvp.models import Box, Detection
from fishing_mvp.vision import color_mask


def test_purple_mask_detects_saturated_purple_without_fixed_coordinates():
    image = np.zeros((200, 300, 3), dtype=np.uint8)
    cv2.circle(image, (173, 141), 28, (190, 50, 220), -1)
    mask = color_mask(image, "purple")
    assert int(cv2.countNonZero(mask)) > 1500


def test_normalized_point_is_derived_from_detected_box():
    detection = Detection(frame_index=0, timestamp_s=0.0, frame_width=1000, frame_height=2000, action_button=Box(400, 1400, 200, 200))
    assert detection.normalized_point() == (0.5, 0.75)
    assert detection.normalized_point(Box(100, 200, 100, 100)) == (0.15, 0.125)

