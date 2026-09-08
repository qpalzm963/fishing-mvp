"""Headless debug overlays and snapshots."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .models import Action, Detection, FishingState


STATE_COLOURS: dict[FishingState, tuple[int, int, int]] = {
    FishingState.UNKNOWN: (128, 128, 128),
    FishingState.WAITING: (220, 180, 30),
    FishingState.PROMPT: (210, 70, 220),
    FishingState.CASTING: (255, 160, 30),
    FishingState.QTE: (30, 190, 230),
    FishingState.QUALITY: (80, 220, 90),
    FishingState.RESULT: (40, 210, 255),
    FishingState.ERROR: (40, 40, 240),
}


def _draw_box(frame: np.ndarray, box, colour: tuple[int, int, int], label: str) -> None:
    if box is None:
        return
    cv2.rectangle(frame, (box.x, box.y), (box.right, box.bottom), colour, 4)
    cv2.putText(
        frame,
        label,
        (box.x, max(24, box.y - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        colour,
        2,
        cv2.LINE_AA,
    )


def draw_overlay(
    frame: np.ndarray,
    detection: Detection,
    state: FishingState,
    action: Action | None = None,
    analysed: bool = True,
) -> np.ndarray:
    output = frame.copy()
    state_colour = STATE_COLOURS.get(state, (255, 255, 255))
    _draw_box(output, detection.action_button, (255, 0, 255) if detection.action_active else (170, 170, 170), "action")
    _draw_box(output, detection.prompt_box, (210, 70, 220), f"prompt {detection.prompt_score:.2f}")
    _draw_box(output, detection.gauge_box, (30, 220, 220), f"gauge {detection.gauge_score:.2f}")
    _draw_box(output, detection.result_box, (40, 210, 255), "result")
    _draw_box(output, detection.continue_box, (0, 230, 80), "continue")

    if detection.gauge_box is not None and detection.gauge_marker_x is not None:
        x = int(detection.gauge_box.x + detection.gauge_marker_x * detection.gauge_box.w)
        cv2.line(output, (x, detection.gauge_box.y - 12), (x, detection.gauge_box.bottom + 12), (30, 30, 255), 5)
    if detection.gauge_box is not None and detection.gauge_target_range is not None:
        low, high = detection.gauge_target_range
        x0 = int(detection.gauge_box.x + low * detection.gauge_box.w)
        x1 = int(detection.gauge_box.x + high * detection.gauge_box.w)
        cv2.line(output, (x0, detection.gauge_box.y - 20), (x1, detection.gauge_box.y - 20), (0, 215, 255), 8)

    if action is not None and action.x is not None and action.y is not None:
        cv2.circle(output, (action.x, action.y), 22, (0, 0, 255), 5)
        cv2.putText(output, "PROPOSED TAP", (action.x + 28, action.y), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 255), 2, cv2.LINE_AA)

    lines = [
        f"state={state.value}  hint={detection.hint.value}  conf={detection.confidence:.2f}",
        f"active={detection.action_active}  motion={detection.water_activity:.3f}  quality={detection.quality or '-'}",
        f"frame={detection.frame_index}  t={detection.timestamp_s:.2f}s  {'analysed' if analysed else 'held'}",
    ]
    if detection.gauge_marker_x is not None:
        lines.append(f"gauge_marker={detection.gauge_marker_x:.3f} target={detection.gauge_target_range or '-'}")
    panel_height = 42 + 34 * len(lines)
    overlay = output.copy()
    cv2.rectangle(overlay, (0, 0), (output.shape[1], panel_height), (10, 10, 10), -1)
    cv2.addWeighted(overlay, 0.76, output, 0.24, 0, output)
    for index, line in enumerate(lines):
        colour = state_colour if index == 0 else (240, 240, 240)
        cv2.putText(output, line, (18, 35 + index * 34), cv2.FONT_HERSHEY_SIMPLEX, 0.82, colour, 2, cv2.LINE_AA)
    return output


def save_snapshot(frame: np.ndarray, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(destination), frame):
        raise RuntimeError(f"Unable to write debug snapshot: {destination}")
