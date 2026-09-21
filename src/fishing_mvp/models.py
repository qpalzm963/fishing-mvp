"""Data models shared by the detector, state machine, and output writers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FishingState(str, Enum):
    UNKNOWN = "unknown"
    WAITING = "waiting"
    PROMPT = "prompt"
    CASTING = "casting"
    QTE = "qte"
    QUALITY = "quality"
    RESULT = "result"
    ERROR = "error"


class ActionType(str, Enum):
    NONE = "none"
    TAP = "tap"
    PRESS = "press"
    RELEASE = "release"


@dataclass(frozen=True)
class FrameMetadata:
    """Capture timing attached to a frame delivered to the detector."""

    source_mode: str
    frame_index: int
    frame_pts_us: int | None = None
    packet_received_at_monotonic: float | None = None
    decoded_at_monotonic: float | None = None
    read_at_monotonic: float | None = None
    reused: bool = False


@dataclass(frozen=True)
class Box:
    """A pixel-space rectangle."""

    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return int(self.x + self.w / 2)

    @property
    def cy(self) -> int:
        return int(self.y + self.h / 2)

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass
class Detection:
    """One observation from one analysed frame."""

    frame_index: int
    timestamp_s: float
    frame_width: int
    frame_height: int
    action_button: Box | None = None
    action_active: bool = False
    action_score: float = 0.0
    prompt_box: Box | None = None
    prompt_score: float = 0.0
    gauge_box: Box | None = None
    gauge_score: float = 0.0
    gauge_marker_x: float | None = None
    gauge_marker_width: float | None = None
    gauge_target_range: tuple[float, float] | None = None
    quality: str | None = None
    quality_score: float = 0.0
    result_box: Box | None = None
    result_score: float = 0.0
    continue_box: Box | None = None
    result_fallback_box: Box | None = None
    water_activity: float = 0.0
    hint: FishingState = FishingState.UNKNOWN
    confidence: float = 0.0
    source_mode: str | None = None
    frame_pts_us: int | None = None
    frame_received_s: float | None = None
    frame_decoded_s: float | None = None
    frame_read_s: float | None = None
    frame_reused: bool | None = None
    frame_age_s: float | None = None
    analysis_duration_s: float | None = None
    features: dict[str, Any] = field(default_factory=dict)
    # Appended after the original fields to preserve positional-constructor
    # compatibility. ``gauge_target_range`` remains the safe-range alias.
    gauge_raw_target_range: tuple[float, float] | None = None
    gauge_tracked_target_range: tuple[float, float] | None = None
    gauge_safe_click_range: tuple[float, float] | None = None

    def normalized_point(self, box: Box | None = None) -> tuple[float, float] | None:
        target = box or self.action_button
        if target is None or self.frame_width <= 0 or self.frame_height <= 0:
            return None
        return (target.cx / self.frame_width, target.cy / self.frame_height)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "frame_index": self.frame_index,
            "timestamp_s": round(self.timestamp_s, 4),
            "frame_size": {"width": self.frame_width, "height": self.frame_height},
            "action_button": self.action_button.to_dict() if self.action_button else None,
            "action_active": self.action_active,
            "action_score": round(self.action_score, 4),
            "prompt_box": self.prompt_box.to_dict() if self.prompt_box else None,
            "prompt_score": round(self.prompt_score, 4),
            "gauge_box": self.gauge_box.to_dict() if self.gauge_box else None,
            "gauge_score": round(self.gauge_score, 4),
            "gauge_marker_x": round(self.gauge_marker_x, 4) if self.gauge_marker_x is not None else None,
            "gauge_marker_width": round(self.gauge_marker_width, 4) if self.gauge_marker_width is not None else None,
            "gauge_target_range": list(self.gauge_target_range) if self.gauge_target_range else None,
            "gauge_raw_target_range": list(self.gauge_raw_target_range) if self.gauge_raw_target_range else None,
            "gauge_tracked_target_range": list(self.gauge_tracked_target_range) if self.gauge_tracked_target_range else None,
            "gauge_safe_click_range": list(self.gauge_safe_click_range) if self.gauge_safe_click_range else None,
            "quality": self.quality,
            "quality_score": round(self.quality_score, 4),
            "result_box": self.result_box.to_dict() if self.result_box else None,
            "result_score": round(self.result_score, 4),
            "continue_box": self.continue_box.to_dict() if self.continue_box else None,
            "result_fallback_box": self.result_fallback_box.to_dict() if self.result_fallback_box else None,
            "water_activity": round(self.water_activity, 4),
            "hint": self.hint.value,
            "confidence": round(self.confidence, 4),
            "source_mode": self.source_mode,
            "frame_pts_us": self.frame_pts_us,
            "frame_received_s": round(self.frame_received_s, 4) if self.frame_received_s is not None else None,
            "frame_decoded_s": round(self.frame_decoded_s, 4) if self.frame_decoded_s is not None else None,
            "frame_read_s": round(self.frame_read_s, 4) if self.frame_read_s is not None else None,
            "frame_reused": self.frame_reused,
            "frame_age_s": round(self.frame_age_s, 4) if self.frame_age_s is not None else None,
            "analysis_duration_s": round(self.analysis_duration_s, 4) if self.analysis_duration_s is not None else None,
            "features": self.features,
        }
        return result


@dataclass(frozen=True)
class Action:
    """An input proposal. Pixel coordinates are derived from a live detection."""

    action_type: ActionType
    x: int | None = None
    y: int | None = None
    x_norm: float | None = None
    y_norm: float | None = None
    hold_ms: int = 0
    reason: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.action_type.value,
            "x": self.x,
            "y": self.y,
            "x_norm": round(self.x_norm, 5) if self.x_norm is not None else None,
            "y_norm": round(self.y_norm, 5) if self.y_norm is not None else None,
            "hold_ms": self.hold_ms,
            "reason": self.reason,
            "confidence": round(self.confidence, 4),
        }


@dataclass(frozen=True)
class StateTransition:
    timestamp_s: float
    frame_index: int
    from_state: FishingState
    to_state: FishingState
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_s": round(self.timestamp_s, 4),
            "frame_index": self.frame_index,
            "from": self.from_state.value,
            "to": self.to_state.value,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
        }
