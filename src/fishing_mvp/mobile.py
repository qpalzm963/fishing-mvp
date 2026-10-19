"""Frame-driven full-auto session for on-device hosts such as the Android app.

The ADB live runner (:mod:`fishing_mvp.live`) owns its capture loop and talks to
the phone over ADB/scrcpy.  An on-device host instead *pushes* frames (for
example from Android MediaProjection) and owns input injection (for example an
AccessibilityService).  :class:`MobileSession` keeps the exact same detector,
state machine, planner, full-auto progress and bounded retry logic, but takes
one frame per :meth:`MobileSession.process_frame` call and delegates input to a
host-supplied dispatcher.

Safety contract (mirrors ``run_live``):

* ``send_actions`` defaults to ``False``; dry-run only records proposals.
* The host must report the foreground package; any mismatch stops the session
  before an input is dispatched.
* UNKNOWN/stage timeouts, frame-size changes and dispatcher errors stop the
  session.  A stopped session never proposes another action.
* Coordinates are always normalized; the host maps them to its real screen.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import yaml

from .config import AppConfig, load_config, validate_config
from .debug import draw_overlay, save_snapshot
from .live import (
    _annotate_frame_timing,
    _capture_fps_for_state,
    _map_action_to_device,
    _observe_qte_actions,
    _timing_record,
)
from .models import Action, FishingState, FrameMetadata
from .retry import AttemptRecovery
from .state_machine import ActionPlanner, AutomationProgress, FishingStateMachine
from .vision import FrameAnalyzer


logger = logging.getLogger(__name__)

SOURCE_MODE = "android_mediaprojection"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "defaults" / "default.yaml"

# Button radius range for phone captures.  The shipped range (4.5%-22% of the frame width) is far
# wider than the real control (about 9%-16%), and the Hough search cost grows with the range:
# measured ~40% faster full analysis with unchanged states.  Applied only to fields the user's
# own config file does not set.
MOBILE_DETECTOR_DEFAULTS = {"button_min_radius_ratio": 0.06, "button_max_radius_ratio": 0.19}


def _user_detector_keys(path: Path | None) -> set[str]:
    if path is None:
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    detector = data.get("detector") if isinstance(data, dict) else None
    return set(detector) if isinstance(detector, dict) else set()


def load_mobile_config(override: Path | None = None) -> AppConfig:
    """Shipped defaults, the phone-specific detector tweaks, then the user's own file on top."""

    config = load_config(override, base_path=DEFAULT_CONFIG_PATH)
    tweaks = {k: v for k, v in MOBILE_DETECTOR_DEFAULTS.items() if k not in _user_detector_keys(override)}
    return replace(config, detector=replace(config.detector, **tweaks))


class TapDispatcher(Protocol):
    """Host input path.  Must raise on failure; returns an input path label."""

    def dispatchTap(self, x_norm: float, y_norm: float, hold_ms: int) -> str:  # noqa: N802 - Java naming
        ...

    # Optional: ``stopRequested() -> bool`` is checked right before dispatch.


def frame_from_rgba(
    data: Any,
    width: int,
    height: int,
    row_stride: int | None = None,
) -> np.ndarray:
    """Convert a packed RGBA_8888 buffer (with optional row padding) to BGR.

    ``data`` may be ``bytes``, ``bytearray``, a ``memoryview``, a NumPy array or a
    Chaquopy Java ``byte[]`` (which supports the buffer protocol).
    """

    import cv2

    if width <= 0 or height <= 0:
        raise ValueError(f"invalid frame size {width}x{height}")
    stride = int(row_stride or width * 4)
    if stride < width * 4:
        raise ValueError(f"row stride {stride} is smaller than width*4 ({width * 4})")
    try:
        buffer = np.frombuffer(data, dtype=np.uint8)
    except TypeError:
        buffer = np.frombuffer(bytes(data), dtype=np.uint8)
    needed = stride * (height - 1) + width * 4
    if buffer.size < needed:
        raise ValueError(f"frame buffer too small: {buffer.size} < {needed}")
    if buffer.size < stride * height:
        # The last row of an Android Image plane may omit its trailing padding.
        buffer = np.concatenate([buffer, np.zeros(stride * height - buffer.size, dtype=np.uint8)])
    rgba = buffer[: stride * height].reshape(height, stride)[:, : width * 4].reshape(height, width, 4)
    return cv2.cvtColor(np.ascontiguousarray(rgba), cv2.COLOR_RGBA2BGR)


class MobileSession:
    """One full-auto session fed by host frames."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        max_rounds: int = 1,
        send_actions: bool = False,
        package: str | None = None,
        config: AppConfig | None = None,
        config_path: str | Path | None = None,
        dispatcher: TapDispatcher | None = None,
        auto_retry: bool | None = None,
        clock=time.monotonic,
    ) -> None:
        if not 1 <= int(max_rounds) <= 999:
            raise ValueError("max_rounds must be between 1 and 999")
        if config is None:
            override = Path(config_path) if config_path and Path(config_path).is_file() else None
            config = load_mobile_config(override)
        validate_config(config)
        if send_actions and dispatcher is None:
            raise ValueError("live mode requires a dispatcher")
        if send_actions and not package:
            raise ValueError("live mode requires the target game package")
        config = replace(config, action=replace(config.action))
        if auto_retry is not None:
            # The host's switch wins over config files so the screen never disagrees with the run.
            config = replace(config, automation=replace(config.automation, auto_retry_enabled=bool(auto_retry)))
        config.action.auto_start = True
        config.action.qte_enabled = True
        config.action.auto_continue = True
        self.config = config
        self.send_actions = bool(send_actions)
        self.package = package
        self.dispatcher = dispatcher
        self.max_rounds = int(max_rounds)
        self._clock = clock
        self.output_dir = Path(output_dir).expanduser().resolve()
        (self.output_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        self.analyzer = FrameAnalyzer(config.detector)
        self.machine = FishingStateMachine(config.state_machine)
        self.planner = ActionPlanner(config.action)
        self.automation = AutomationProgress(max_rounds=self.max_rounds)
        self.recovery = AttemptRecovery(config, self.automation)
        self.start = clock()
        self.state = FishingState.UNKNOWN
        self.frame_index = 0
        self.first_size: tuple[int, int] | None = None
        self.device_size: tuple[int, int] | None = None
        self.transitions: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.pending_qte_actions: list[dict[str, Any]] = []
        self.state_counts: dict[str, int] = {}
        self.input_path_counts: dict[str, int] = {}
        self.warning_counts: dict[str, int] = {}
        self.analysis_ms_total = 0.0
        self.stop_reason: str | None = None
        self.error: dict[str, str] | None = None
        self.log_path = self.output_dir / "live_detections.jsonl"
        self._log = self.log_path.open("w", encoding="utf-8")
        self._closed = False

    # ------------------------------------------------------------------ host API
    @property
    def stopped(self) -> bool:
        return self.stop_reason is not None

    def set_device_size(self, width: int, height: int) -> None:
        """Real (physical, current-orientation) screen size used for taps."""

        self.device_size = (int(width), int(height))

    def next_interval_s(self) -> float:
        return 1.0 / _capture_fps_for_state(self.state, self.config)

    def stop(self, reason: str = "user_stop") -> str:
        if self.stop_reason is None:
            self.stop_reason = reason
        return self.finish()

    def process_rgba(
        self,
        data: Any,
        width: int,
        height: int,
        row_stride: int,
        captured_at_monotonic: float | None = None,
        reused: bool = False,
        foreground_package: str | None = None,
    ) -> str:
        """Host entry point for Android RGBA_8888 frames.  Returns JSON."""

        if self.stopped:
            return json.dumps(self._result(None, None, None), ensure_ascii=False)
        try:
            frame = frame_from_rgba(data, width, height, row_stride)
        except Exception as exc:  # malformed frame is an infrastructure error
            self._fail(exc)
            return json.dumps(self._result(None, None, None), ensure_ascii=False)
        return json.dumps(
            self.process_frame(
                frame,
                captured_at_monotonic=captured_at_monotonic,
                reused=reused,
                foreground_package=foreground_package,
            ),
            ensure_ascii=False,
        )

    def process_frame(
        self,
        frame: np.ndarray,
        *,
        captured_at_monotonic: float | None = None,
        reused: bool = False,
        foreground_package: str | None = None,
    ) -> dict[str, Any]:
        """Analyse one BGR frame, plan, optionally dispatch, and log it."""

        if self.stopped:
            return self._result(None, None, None)
        try:
            return self._process(frame, captured_at_monotonic, reused, foreground_package)
        except Exception as exc:
            self._fail(exc)
            return self._result(None, None, None)

    # --------------------------------------------------------------- internals
    def _fail(self, exc: Exception) -> None:
        logger.error("mobile session failed: %s", exc)
        self.stop_reason = "error"
        self.error = {"type": type(exc).__name__, "message": str(exc)}

    def _check_foreground(self, foreground_package: str | None) -> None:
        if self.package and foreground_package != self.package:
            raise RuntimeError(
                f"Foreground package changed; stopping before sending action "
                f"(expected={self.package}, foreground={foreground_package})"
            )

    def _process(
        self,
        frame: np.ndarray,
        captured_at_monotonic: float | None,
        reused: bool,
        foreground_package: str | None,
    ) -> dict[str, Any]:
        config = self.config
        self._check_foreground(foreground_package)
        size = (int(frame.shape[1]), int(frame.shape[0]))
        if self.first_size is None:
            self.first_size = size
        if size != self.first_size:
            raise RuntimeError(f"Screen size changed during live run: {self.first_size} -> {size}")
        metadata = FrameMetadata(
            source_mode=SOURCE_MODE,
            frame_index=self.frame_index,
            frame_pts_us=int(captured_at_monotonic * 1_000_000) if captured_at_monotonic is not None else None,
            decoded_at_monotonic=captured_at_monotonic,
            read_at_monotonic=self._clock(),
            reused=bool(reused),
        )
        analysis_started_at = self._clock()
        timestamp_s = analysis_started_at - self.start
        recovery = self.recovery
        detection = self.analyzer.analyze(
            frame,
            self.frame_index,
            timestamp_s,
            # Track the remembered button locally in every state: the analyzer verifies it from the
            # current pixels and falls back to the full search when it is not there (for example on
            # the result screen), so this keeps the first QTE frame as cheap as the later ones.
            fast=not recovery.recovering,
        )
        analysis_finished_at = self._clock()
        _annotate_frame_timing(
            detection,
            metadata,
            start_monotonic=self.start,
            analysis_started_at=analysis_started_at,
            analysis_finished_at=analysis_finished_at,
        )
        self.analysis_ms_total += (analysis_finished_at - analysis_started_at) * 1000.0
        state, transition = self.machine.update(detection)
        self.state = state
        retry_event_start = len(recovery.events)
        if recovery.update(detection, state, transition):
            for pending in self.pending_qte_actions:
                pending["observation_status"] = "attempt_failed"
            self.pending_qte_actions.clear()
            self.analyzer = FrameAnalyzer(config.detector)
            self.machine = FishingStateMachine(config.state_machine)
            self.planner = ActionPlanner(config.action)
        qte_observations = _observe_qte_actions(
            self.pending_qte_actions,
            detection,
            transition,
            timeout_s=max(config.automation.qte_timeout_s, config.automation.quality_timeout_s),
        )
        automation = self.automation
        blocked = automation.stop_reason is not None or recovery.recovering
        action = None if blocked else self.planner.plan(
            detection, state, transition, start_allowed=automation.start_allowed,
        )
        self.state_counts[state.value] = self.state_counts.get(state.value, 0) + 1
        record: dict[str, Any] = {
            "state": state.value,
            "detection": detection.to_dict(),
            "action": action.to_dict() if action else None,
            "timing": _timing_record(
                detection,
                analysis_started_at=analysis_started_at,
                analysis_finished_at=analysis_finished_at,
                start_monotonic=self.start,
            ),
            "attempt": recovery.attempt,
            "recovering": recovery.recovering,
        }
        if qte_observations:
            record["qte_observations"] = qte_observations
        retry_events = recovery.events[retry_event_start:]
        if retry_events:
            record["retry_events"] = retry_events
        if transition is not None:
            transition_dict = transition.to_dict()
            self.transitions.append(transition_dict)
            record["transition"] = transition_dict
        sent_action: Action | None = None
        error: Exception | None = None
        if action is not None:
            target_size = self.device_size or size
            if self.send_actions and self.device_size is None and action.x_norm is not None:
                raise RuntimeError("Device display size is unavailable; refusing to send normalized coordinates")
            sent_action = _map_action_to_device(action, size, target_size)
            action_record: dict[str, Any] = {
                "timestamp_s": timestamp_s,
                "loop_frame_index": self.frame_index,
                **action.to_dict(),
                "sent": self.send_actions,
                "sent_action": sent_action.to_dict() if self.send_actions else None,
                "input_path": "dry_run",
                "attempt": recovery.attempt,
                "analysis_start_s": round(timestamp_s, 4),
                "frame_pts_us": detection.frame_pts_us,
                "frame_age_ms": round(detection.frame_age_s * 1000.0, 2) if detection.frame_age_s is not None else None,
            }
            stop_check = getattr(self.dispatcher, "stopRequested", None)
            if self.send_actions and callable(stop_check) and stop_check():
                # A stop may arrive during analysis; never dispatch after it.
                self.stop_reason = "user_stop"
                record["action_sent"] = False
                record["stop_before_dispatch"] = True
                self._log.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._log.flush()
                self.frame_index += 1
                return self._result(state, action, None, detection.confidence, record["timing"]["analysis_ms"])
            if self.send_actions:
                assert self.dispatcher is not None
                dispatch_started = self._clock()
                try:
                    input_path = str(
                        self.dispatcher.dispatchTap(
                            float(action.x_norm if action.x_norm is not None else 0.5),
                            float(action.y_norm if action.y_norm is not None else 0.5),
                            int(max(0, action.hold_ms)),
                        )
                    )
                except Exception as exc:  # host refused or failed the input
                    error = exc
                    input_path = "error"
                dispatch_finished = self._clock()
                execution_ms = (dispatch_finished - dispatch_started) * 1000.0
                action_record.update(
                    input_path=input_path,
                    execution_ms=round(execution_ms, 2),
                    dispatch_start_s=round(dispatch_started - self.start, 4),
                    dispatch_end_s=round(dispatch_finished - self.start, 4),
                )
                record.update(
                    action_sent=error is None,
                    sent_action=sent_action.to_dict(),
                    action_execution_ms=round(execution_ms, 2),
                    input_path=input_path,
                )
                if error is None:
                    self.input_path_counts[input_path] = self.input_path_counts.get(input_path, 0) + 1
                    if state == FishingState.QTE:
                        self.planner.record_input_dispatch(SOURCE_MODE, execution_ms / 1000.0)
                    if state == FishingState.WAITING:
                        recovery.record_start_action(
                            detection, sent_action, input_path, dispatch_finished - self.start,
                        )
            else:
                record["action_sent"] = False
            if state == FishingState.QTE:
                self.pending_qte_actions.append(action_record)
            self.actions.append(action_record)
        if transition is not None:
            try:
                save_snapshot(
                    draw_overlay(frame, detection, state, action),
                    self.output_dir / "snapshots" / f"{len(self.transitions):03d}_{state.value}_{timestamp_s:07.2f}.jpg",
                )
            except Exception as exc:
                self.warning_counts["snapshot_failed"] = self.warning_counts.get("snapshot_failed", 0) + 1
                record["snapshot_error"] = str(exc)
        self._log.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._log.flush()
        self.frame_index += 1
        if error is not None:
            raise RuntimeError(f"Input dispatch failed: {error}")
        if automation.stop_reason is not None:
            self.stop_reason = automation.stop_reason
        return self._result(state, action, sent_action, detection.confidence, record["timing"]["analysis_ms"])

    def _result(
        self,
        state: FishingState | None,
        action: Action | None,
        sent_action: Action | None,
        confidence: float = 0.0,
        analysis_ms: float | None = None,
    ) -> dict[str, Any]:
        return {
            "state": (state or self.state).value,
            "confidence": round(confidence, 4),
            "frame_index": self.frame_index,
            "completed_rounds": self.automation.completed_rounds,
            "max_rounds": self.max_rounds,
            "attempt": self.recovery.attempt,
            "recovering": self.recovery.recovering,
            "action": action.to_dict() if action else None,
            "sent": bool(self.send_actions and sent_action is not None and self.error is None),
            "analysis_ms": analysis_ms,
            "next_interval_s": round(self.next_interval_s(), 4),
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
            "error": self.error,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "host": "android",
            "package": self.package,
            "send_actions": self.send_actions,
            "full_auto": True,
            "max_rounds": self.max_rounds,
            "completed_rounds": self.automation.completed_rounds,
            "retry": self.recovery.summary(),
            "stop_reason": self.stop_reason,
            "error": self.error,
            "last_state": self.state.value,
            "frame_size": list(self.first_size) if self.first_size else None,
            "device_size": list(self.device_size) if self.device_size else None,
            "frame_count": self.frame_index,
            "mean_analysis_ms": round(self.analysis_ms_total / self.frame_index, 2) if self.frame_index else None,
            "state_counts": self.state_counts,
            "transitions": self.transitions,
            "actions": self.actions,
            "input_path_counts": self.input_path_counts,
            "warning_counts": self.warning_counts,
            "qte_dispatch_latency_estimates_ms": self.planner.latency_estimates(),
            "log": str(self.log_path),
            "notes": [
                "Frames come from Android MediaProjection; inputs from AccessibilityService gestures.",
                "Dry-run unless live mode was explicitly enabled in the overlay.",
                "Stops on foreground package change, screen-size change, timeouts or input errors.",
            ],
        }

    def finish(self) -> str:
        """Close logs and atomically write ``live_summary.json``.  Returns JSON."""

        summary = self.summary()
        if not self._closed:
            self._closed = True
            for pending in self.pending_qte_actions:
                pending["observation_status"] = "session_stopped"
            self.pending_qte_actions.clear()
            if self.stop_reason not in {None, "completed_rounds"}:
                self.recovery.events.append({
                    "attempt": self.recovery.attempt,
                    "retry_attempts": self.recovery.retries,
                    "decision": "session_stopped",
                    "stop_reason": self.stop_reason,
                    "last_state": self.state.value,
                    "error": self.error,
                    "recoverable": False,
                })
                summary = self.summary()
            try:
                self._log.close()
            finally:
                path = self.output_dir / "live_summary.json"
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(path)
        return json.dumps(summary, ensure_ascii=False)


def create_session(
    output_dir: str,
    max_rounds: int,
    send_actions: bool,
    package: str | None,
    config_path: str | None,
    dispatcher: TapDispatcher | None,
    auto_retry: bool | None = None,
) -> MobileSession:
    """Positional factory that is convenient to call from Kotlin via Chaquopy."""

    return MobileSession(
        output_dir,
        max_rounds=int(max_rounds),
        send_actions=bool(send_actions),
        package=package or None,
        config_path=config_path or None,
        dispatcher=dispatcher,
        auto_retry=None if auto_retry is None else bool(auto_retry),
    )


SELF_TEST_FIXTURES = ("waiting", "prompt", "qte", "quality", "result")


def self_test(fixture_dir: str, repeats: int = 3) -> str:
    """Run the detector on bundled fixture frames; return JSON diagnostics.

    Used by the Android app's "self test" button to confirm that Python, NumPy
    and OpenCV load on the device and to measure per-frame analysis time.
    """

    import platform

    import cv2

    from .config import load_config

    config = load_mobile_config()
    report: dict[str, Any] = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "numpy": np.__version__,
        "opencv": cv2.__version__,
        "frames": [],
    }
    for name in SELF_TEST_FIXTURES:
        path = Path(fixture_dir) / f"{name}.jpg"
        frame = cv2.imread(str(path))
        if frame is None:
            report["frames"].append({"name": name, "error": f"unreadable: {path}"})
            continue
        timings = []
        detection = None
        for index in range(max(1, int(repeats))):
            analyzer = FrameAnalyzer(config.detector)
            started = time.perf_counter()
            detection = analyzer.analyze(frame, index, index * 0.1)
            timings.append((time.perf_counter() - started) * 1000.0)
        assert detection is not None
        report["frames"].append({
            "name": name,
            "size": [int(frame.shape[1]), int(frame.shape[0])],
            "hint": detection.hint.value,
            "confidence": round(detection.confidence, 3),
            "action_button": detection.action_button.to_dict() if detection.action_button else None,
            "gauge": detection.gauge_box is not None,
            "analysis_ms_min": round(min(timings), 2),
            "analysis_ms_max": round(max(timings), 2),
        })
    report["ok"] = all(item.get("hint") == item["name"] for item in report["frames"])
    return json.dumps(report, ensure_ascii=False)
