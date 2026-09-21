"""Configuration loading with relative, resolution-independent defaults."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
import math
from pathlib import Path
from typing import Any, get_type_hints

import yaml


@dataclass
class DetectorConfig:
    max_work_width: int = 480
    purple_hue_low: int = 125
    purple_hue_high: int = 179
    min_saturation: int = 45
    min_value: int = 45
    button_min_radius_ratio: float = 0.045
    button_max_radius_ratio: float = 0.22
    button_min_y_ratio: float = 0.57
    button_max_y_ratio: float = 0.91
    button_active_purple_ratio: float = 0.08
    button_active_saturation: float = 42.0
    prompt_min_fill_ratio: float = 0.10
    prompt_min_aspect: float = 2.0
    prompt_max_aspect: float = 12.0
    gauge_min_aspect: float = 2.4
    gauge_min_width_button_radius: float = 2.2
    gauge_min_color_pixels: int = 80
    # QTE targets can be much narrower than the original 10% span. Keep
    # pixel and column-continuity guards so isolated yellow UI noise is not a
    # target just because it passes the relaxed width threshold.
    gauge_target_min_color_pixels: int = 24
    gauge_target_min_width_ratio: float = 0.025
    gauge_target_min_width_px: int = 6
    gauge_target_min_column_coverage: float = 0.45
    gauge_marker_max_width_ratio: float = 0.35
    # A moving red marker can temporarily occlude a narrow yellow target.
    # Keep a short, bounded relative envelope instead of trusting one frame.
    gauge_target_tracking_frames: int = 4
    gauge_target_tracking_max_width_ratio: float = 0.18
    gauge_target_tracking_max_gap_ratio: float = 0.06
    gauge_target_tracking_missing_frames: int = 4
    gauge_min_state_score: float = 0.72
    quality_min_pixels: int = 180
    result_dark_luma: float = 112.0
    result_yellow_ratio: float = 0.010
    motion_threshold: int = 22
    motion_min_ratio: float = 0.04
    # Locate the gauge on the work frame, then refine marker/target colour
    # spans inside the mapped original-resolution ROI.  Appended to preserve
    # positional compatibility with the original detector config.
    gauge_full_res_refine_enabled: bool = True
    gauge_full_res_refine_padding_ratio: float = 0.12


@dataclass
class StateMachineConfig:
    stable_frames: int = 2
    unknown_grace_frames: int = 8
    result_min_hold_s: float = 2.0


@dataclass
class ActionConfig:
    # Zero lets Android deliver an input tap immediately.  A positive value
    # uses a same-point swipe and is intentionally opt-in for slower devices.
    tap_hold_ms: int = 0
    auto_start: bool = False
    qte_enabled: bool = False
    auto_continue: bool = False
    min_confidence: float = 0.68
    min_action_interval_s: float = 0.45
    qte_min_interval_s: float = 0.18
    qte_target_margin: float = 0.05
    # Estimated time from the analysed frame to the Android touch event.
    # The planner predicts marker motion over this horizon instead of waiting
    # for the marker to visibly enter the target range.
    qte_input_latency_s: float = 0.18
    qte_latency_sample_window: int = 5
    qte_velocity_samples: int = 3
    qte_min_velocity_norm_s: float = 0.12
    # If a predictive tap lands before the marker is visually observed in the
    # target, re-arm after this grace window so a later sweep can be retried.
    qte_prediction_grace_s: float = 0.24
    # Keep result recovery bounded: retry a detected continue/dismiss control
    # only while the result overlay is still visibly present.
    result_extra_tap_enabled: bool = True
    result_extra_tap_delay_s: float = 1.0
    result_max_attempts: int = 3
    # Appended after the original action fields for positional compatibility.
    # ETA uses the existing predicted-position check as its fallback.
    qte_eta_enabled: bool = True
    # auto reflects only when a predicted path crosses [0, 1]; clip preserves
    # the legacy non-bouncing behaviour for games that do not reflect.
    qte_boundary_mode: str = "auto"
    qte_velocity_max_jitter_norm_s: float = 0.8


@dataclass
class AutomationConfig:
    """Fail-safe stage timeouts used by the explicit full-auto runner."""

    unknown_timeout_s: float = 8.0
    waiting_timeout_s: float = 30.0
    unconfirmed_waiting_timeout_s: float = 8.0
    prompt_timeout_s: float = 12.0
    casting_timeout_s: float = 12.0
    qte_timeout_s: float = 35.0
    quality_timeout_s: float = 10.0
    result_timeout_s: float = 12.0
    # Foreground checks are intentionally periodic: a dumpsys call before
    # every QTE tap adds avoidable input latency.
    foreground_check_interval_s: float = 0.75


@dataclass
class ScrcpyConfig:
    max_size: int = 0
    max_fps: int = 30
    video_bit_rate: int = 8_000_000
    connect_timeout_s: float = 10.0
    frame_timeout_s: float = 3.0


@dataclass
class AppConfig:
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    state_machine: StateMachineConfig = field(default_factory=StateMachineConfig)
    action: ActionConfig = field(default_factory=ActionConfig)
    automation: AutomationConfig = field(default_factory=AutomationConfig)
    scrcpy: ScrcpyConfig = field(default_factory=ScrcpyConfig)
    capture_fps: float = 10.0
    qte_capture_fps: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _update_dataclass(instance: Any, values: Any, prefix: str = "") -> Any:
    if not isinstance(values, dict):
        raise ValueError(f"{prefix or 'config'}: expected a mapping")
    valid = get_type_hints(type(instance))
    for key, value in values.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if key not in valid:
            raise ValueError(f"{name}: unknown config field")
        current = getattr(instance, key)
        if is_dataclass(current):
            _update_dataclass(current, value, name)
        else:
            _validate_type(name, value, valid[key])
            setattr(instance, key, float(value) if valid[key] is float else value)
    return instance


def _validate_type(name: str, value: Any, expected: type) -> None:
    allowed = (int, float) if expected is float else (expected,)
    if type(value) not in allowed:
        raise ValueError(f"{name}: expected {expected.__name__}, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name}: must be finite")


def validate_config(config: AppConfig) -> None:
    """Reject unsafe types, ranges and inverted bounds before device access."""
    positive = {
        "detector.max_work_width", "detector.gauge_min_color_pixels",
        "detector.gauge_target_min_color_pixels", "detector.gauge_target_min_width_px",
        "detector.gauge_target_tracking_frames", "detector.quality_min_pixels",
        "detector.prompt_min_aspect", "detector.prompt_max_aspect",
        "detector.gauge_min_aspect", "detector.gauge_min_width_button_radius",
        "state_machine.stable_frames", "action.qte_latency_sample_window",
        "action.qte_velocity_samples", "action.result_max_attempts",
        "scrcpy.max_fps", "scrcpy.video_bit_rate",
        "scrcpy.connect_timeout_s", "scrcpy.frame_timeout_s",
    }
    unit_fields = {
        "button_min_y_ratio", "button_max_y_ratio", "prompt_min_fill_ratio",
        "gauge_target_min_column_coverage", "gauge_min_state_score",
        "min_confidence", "qte_target_margin",
    }
    byte_fields = {"min_saturation", "min_value", "button_active_saturation", "result_dark_luma", "motion_threshold"}

    def check(instance: Any, prefix: str = "") -> None:
        types = get_type_hints(type(instance))
        for item in fields(instance):
            value = getattr(instance, item.name)
            name = f"{prefix}.{item.name}" if prefix else item.name
            if is_dataclass(value):
                check(value, name)
                continue
            _validate_type(name, value, types[item.name])
            if name == "action.qte_boundary_mode":
                if value not in {"auto", "reflection", "clip"}:
                    raise ValueError(f"{name}: must be auto, reflection or clip")
                continue
            if isinstance(value, bool):
                continue
            if value < 0 or (name in positive and value == 0):
                raise ValueError(f"{name}: must be {'positive' if name in positive else 'non-negative'}")
            if item.name.endswith("_ratio") or item.name in unit_fields:
                if value > 1:
                    raise ValueError(f"{name}: must be between 0 and 1")
            if item.name in byte_fields and value > 255:
                raise ValueError(f"{name}: must be between 0 and 255")
            if item.name.startswith("purple_hue_") and value > 179:
                raise ValueError(f"{name}: must be between 0 and 179")
            if name in {"capture_fps", "qte_capture_fps"} and value < 0.5:
                raise ValueError(f"{name}: must be at least 0.5")

    check(config)
    for low, high in (
        ("purple_hue_low", "purple_hue_high"),
        ("button_min_radius_ratio", "button_max_radius_ratio"),
        ("button_min_y_ratio", "button_max_y_ratio"),
        ("prompt_min_aspect", "prompt_max_aspect"),
    ):
        if getattr(config.detector, low) > getattr(config.detector, high):
            raise ValueError(f"detector.{low}: must not exceed detector.{high}")


def load_config(
    path: str | Path | None = None,
    *,
    base_path: str | Path | None = None,
) -> AppConfig:
    """Load a packaged/default config and then an optional override file.

    Existing callers that pass one config path retain the original behavior.
    The portable launcher uses ``base_path`` for the packaged baseline and
    ``path`` for an operator-owned ``config/user.yaml`` override.
    """

    config = AppConfig(
        detector=DetectorConfig(),
        state_machine=StateMachineConfig(),
        action=ActionConfig(),
    )

    def apply_file(config_path: str | Path) -> None:
        config_path = Path(config_path)
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            _update_dataclass(config, {} if data is None else data)
        except (ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"{config_path}: {exc}") from exc

    if base_path is not None:
        apply_file(base_path)
    if path is not None:
        apply_file(path)
    validate_config(config)
    return config
