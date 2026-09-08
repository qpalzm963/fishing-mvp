"""Configuration loading with relative, resolution-independent defaults."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

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
    gauge_min_state_score: float = 0.72
    quality_min_pixels: int = 180
    result_dark_luma: float = 112.0
    result_yellow_ratio: float = 0.010
    motion_threshold: int = 22
    motion_min_ratio: float = 0.04


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
    qte_velocity_samples: int = 3
    qte_min_velocity_norm_s: float = 0.12


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _update_dataclass(instance: Any, values: dict[str, Any]) -> Any:
    valid = {item.name for item in fields(instance)}
    for key, value in values.items():
        if key in valid:
            setattr(instance, key, value)
    return instance


def load_config(path: str | Path | None = None) -> AppConfig:
    config = AppConfig(
        detector=DetectorConfig(),
        state_machine=StateMachineConfig(),
        action=ActionConfig(),
    )
    if path is None:
        return config
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a mapping: {config_path}")
    _update_dataclass(config.detector, data.get("detector", {}))
    _update_dataclass(config.state_machine, data.get("state_machine", {}))
    _update_dataclass(config.action, data.get("action", {}))
    _update_dataclass(config.automation, data.get("automation", {}))
    _update_dataclass(config.scrcpy, data.get("scrcpy", {}))
    if "capture_fps" in data:
        config.capture_fps = float(data["capture_fps"])
    return config
