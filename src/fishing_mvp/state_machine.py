"""Temporal smoothing and conservative action proposals."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import math
from statistics import median

from .config import ActionConfig, AutomationConfig, StateMachineConfig
from .models import Action, ActionType, Detection, FishingState, StateTransition


class AutomationPhase(str, Enum):
    """Coarse automation phases used for fail-safe timeout tracking."""

    UNKNOWN = "unknown"
    WAITING = "waiting"
    PROMPT = "prompt"
    CASTING = "casting"
    FISHING_QTE = "fishing_qte"
    RESULT = "result"
    ERROR = "error"


class FishingStateMachine:
    def __init__(self, config: StateMachineConfig):
        self.config = config
        self.current = FishingState.UNKNOWN
        self.pending = FishingState.UNKNOWN
        self.pending_count = 0
        self.unknown_streak = 0
        self.last_transition_timestamp = -1.0

    def update(self, detection: Detection) -> tuple[FishingState, StateTransition | None]:
        raw = detection.hint
        if raw == FishingState.UNKNOWN:
            self.unknown_streak += 1
            if self.current != FishingState.UNKNOWN and self.unknown_streak <= self.config.unknown_grace_frames:
                raw = self.current
        else:
            self.unknown_streak = 0

        # A dropped gauge/quality observation during the same QTE animation
        # can momentarily look like water motion.  Keep the temporal context
        # instead of exposing a spurious casting state; the next stable
        # waiting/result observation is still allowed to end the sequence.
        if raw == FishingState.CASTING and self.current in {FishingState.QTE, FishingState.QUALITY}:
            raw = self.current

        if (
            self.current == FishingState.RESULT
            and self.last_transition_timestamp >= 0
            and detection.timestamp_s - self.last_transition_timestamp < self.config.result_min_hold_s
            and raw != FishingState.ERROR
        ):
            raw = FishingState.RESULT

        if raw != self.pending:
            self.pending = raw
            self.pending_count = 1
        else:
            self.pending_count += 1

        transition: StateTransition | None = None
        if self.pending_count >= max(1, self.config.stable_frames) and self.pending != self.current:
            previous = self.current
            self.current = self.pending
            self.last_transition_timestamp = detection.timestamp_s
            transition = StateTransition(
                timestamp_s=detection.timestamp_s,
                frame_index=detection.frame_index,
                from_state=previous,
                to_state=self.current,
                confidence=detection.confidence,
                reason=self._reason(detection),
            )
        return self.current, transition

    @staticmethod
    def _reason(detection: Detection) -> str:
        parts: list[str] = [f"hint={detection.hint.value}"]
        if detection.action_button:
            parts.append(f"button={detection.action_score:.2f}")
        if detection.prompt_box:
            parts.append(f"prompt={detection.prompt_score:.2f}")
        if detection.gauge_box:
            parts.append(f"gauge={detection.gauge_score:.2f}")
        if detection.quality:
            parts.append(f"quality={detection.quality}")
        if detection.result_box:
            parts.append(f"result={detection.result_score:.2f}")
        if detection.water_activity:
            parts.append(f"motion={detection.water_activity:.3f}")
        return ",".join(parts)


@dataclass
class ActionPlanner:
    config: ActionConfig
    last_action_timestamp: float = -1.0
    last_qte_timestamp: float = -1.0
    last_result_timestamp: float = -1.0
    start_handled: bool = False
    prompt_handled: bool = False
    qte_in_target: bool = False
    qte_seen_inside: bool = False
    qte_predicted_only: bool = False
    qte_samples: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=8))
    dispatch_latency_samples: dict[str, deque[float]] = field(default_factory=dict)
    last_qte_diagnostics: dict[str, object] = field(default_factory=dict)
    result_handled: bool = False
    result_extra_tap_handled: bool = False
    result_attempts: int = 0

    def plan(
        self,
        detection: Detection,
        state: FishingState,
        transition: StateTransition | None = None,
        start_allowed: bool = True,
    ) -> Action | None:
        if state != FishingState.WAITING:
            self.start_handled = False
        if state != FishingState.PROMPT:
            self.prompt_handled = False
        if state != FishingState.QTE:
            self.qte_in_target = False
            self.qte_seen_inside = False
            self.qte_predicted_only = False
            self.qte_samples.clear()
        if state != FishingState.RESULT:
            self.result_handled = False
            self.result_extra_tap_handled = False
            self.result_attempts = 0
        if detection.confidence < self.config.min_confidence:
            return None
        if (
            state == FishingState.WAITING
            and self.config.auto_start
            and start_allowed
            and not self.start_handled
            and detection.action_button is not None
            and detection.prompt_box is None
            and detection.gauge_box is None
            and self._allowed(detection.timestamp_s, self.last_action_timestamp, self.config.min_action_interval_s)
        ):
            self.start_handled = True
            self.last_action_timestamp = detection.timestamp_s
            return self._tap_for_box(
                detection,
                detection.action_button,
                "stable dynamically detected waiting/start control",
            )

        if (
            state == FishingState.PROMPT
            and not self.prompt_handled
            and detection.action_button is not None
            and detection.action_active
            and self._allowed(detection.timestamp_s, self.last_action_timestamp, self.config.min_action_interval_s)
        ):
            self.prompt_handled = True
            self.last_action_timestamp = detection.timestamp_s
            return self._tap_for_box(
                detection,
                detection.action_button,
                "stable purple prompt/action control",
            )

        if (
            state == FishingState.QTE
            and self.config.qte_enabled
            and detection.action_button is not None
        ):
            target = self._target_for_detection(detection)
            if detection.gauge_marker_x is None or target is None:
                self._set_qte_diagnostics(
                    detection,
                    decision="missing_geometry",
                    tap_reason="",
                    marker_x=detection.gauge_marker_x,
                    marker_width=detection.gauge_marker_width,
                    raw_target_range=self._range_for_detection(detection, "gauge_raw_target_range"),
                    tracked_target_range=self._range_for_detection(detection, "gauge_tracked_target_range"),
                    safe_click_range=self._range_for_detection(detection, "gauge_safe_click_range") or self._range_value(detection.gauge_target_range),
                )
            else:
                prediction = self._predict_marker_details(detection)
                predicted = prediction["predicted"]
                velocity = prediction["velocity"]
                horizon = float(prediction["horizon"])
                in_target = self._marker_in_target(
                    detection.gauge_marker_x,
                    target,
                    detection.gauge_marker_width,
                )
                eta = None
                timing_window = None
                eta_mode = "disabled"
                if prediction["velocity_stable"]:
                    eta, timing_window, eta_mode = self._eta_to_target(
                        detection.gauge_marker_x,
                        velocity,
                        target,
                        detection.gauge_marker_width,
                        horizon,
                    )

                if self.qte_in_target and in_target:
                    self.qte_seen_inside = True
                    self.qte_predicted_only = False
                elif self.qte_in_target and self.qte_seen_inside and not in_target:
                    # A complete target crossing re-arms the next sweep.
                    self.qte_in_target = False
                    self.qte_seen_inside = False
                    self.qte_predicted_only = False
                elif (
                    self.qte_in_target
                    and self.qte_predicted_only
                    and self.last_qte_timestamp >= 0
                    and detection.timestamp_s - self.last_qte_timestamp >= max(0.0, self.config.qte_prediction_grace_s)
                ):
                    # A predictive tap can miss without the marker ever being
                    # observed inside the target. Re-arm after a short grace
                    # window so a later sweep can produce a guarded retry.
                    self.qte_in_target = False
                    self.qte_seen_inside = False
                    self.qte_predicted_only = False

                should_tap = False
                reason = ""
                decision = "outside_target"
                eta_match = False
                if self.qte_in_target:
                    if in_target:
                        decision = "target_hold"
                        reason = "marker remains in target; QTE re-arm held"
                    elif self.qte_predicted_only:
                        decision = "prediction_grace"
                        reason = "waiting for predictive tap grace window before re-arm"
                if not self.qte_in_target:
                    if (
                        in_target
                        and (
                            not prediction["velocity_stable"]
                        )
                    ):
                        # The first QTE frame has no velocity history.  An
                        # immediate in-range tap is safer than waiting for a
                        # second frame and missing a short target window.
                        should_tap = True
                        decision = "current_in_target"
                        reason = "marker entered target without stable velocity estimate"
                    elif (
                        self.config.qte_eta_enabled
                        and eta is not None
                        and timing_window is not None
                    ):
                        eta_match = eta >= 0.0 and abs(eta - horizon) <= timing_window
                        should_tap = eta_match
                        decision = "eta_window" if eta_match else "eta_outside_window"
                        reason = (
                            ("ETA predicted marker in target " if eta_match else "ETA outside timing window ")
                            + f"(now={detection.gauge_marker_x:.3f},eta={eta:.3f},"
                            f"input_eta={horizon:.3f},window={timing_window:.3f},"
                            f"v={velocity:+.3f}/s,mode={eta_mode})"
                        )
                    elif predicted is not None:
                        should_tap = self._marker_in_target(
                            predicted,
                            target,
                            detection.gauge_marker_width,
                        )
                        decision = "predicted_position" if should_tap else "predicted_outside_target"
                        reason = (
                            ("predicted marker in target " if should_tap else "predicted marker outside target ")
                            + f"(now={detection.gauge_marker_x:.3f},pred={predicted:.3f},"
                            f"v={velocity:+.3f}/s,horizon={horizon * 1000:.0f}ms)"
                        )
                    else:
                        decision = "outside_target"
                        reason = "marker outside target without stable velocity estimate"

                self._set_qte_diagnostics(
                    detection,
                    decision=decision,
                    tap_reason=reason,
                    raw_target_range=self._range_for_detection(detection, "gauge_raw_target_range"),
                    tracked_target_range=self._range_for_detection(detection, "gauge_tracked_target_range"),
                    safe_click_range=self._range_value(target),
                    marker_x=round(float(detection.gauge_marker_x), 5),
                    marker_width=(round(float(detection.gauge_marker_width), 5) if detection.gauge_marker_width is not None else None),
                    velocity=(round(float(velocity), 5) if velocity is not None else None),
                    eta=(round(float(eta), 5) if eta is not None else None),
                    input_eta=round(horizon, 5),
                    timing_window=(round(float(timing_window), 5) if timing_window is not None else None),
                    frame_age_s=detection.frame_age_s,
                    analysis_duration_s=detection.analysis_duration_s,
                    dispatch_latency_s=round(float(prediction["dispatch_latency"]), 5),
                    velocity_jitter=round(float(prediction["velocity_jitter"]), 5),
                    velocity_stable=prediction["velocity_stable"],
                    predicted_position=(round(float(predicted), 5) if predicted is not None else None),
                    prediction_raw_position=(round(float(prediction["raw_predicted"]), 5) if prediction["raw_predicted"] is not None else None),
                    boundary_mode=prediction["boundary_mode"],
                    reflection_applied=prediction["reflection_applied"],
                    eta_match=eta_match,
                )
                if (
                    should_tap
                    and self._allowed(detection.timestamp_s, self.last_qte_timestamp, self.config.qte_min_interval_s)
                ):
                    self.qte_in_target = True
                    self.qte_seen_inside = in_target
                    self.qte_predicted_only = not in_target
                    self.last_qte_timestamp = detection.timestamp_s
                    return self._tap_for_box(detection, detection.action_button, reason)

        if (
            state == FishingState.RESULT
            and self.config.auto_continue
            # The state machine deliberately holds RESULT for a short
            # animation window. Do not tap again when the raw detector has
            # already left the result overlay during that hold.
            and detection.result_box is not None
        ):
            max_attempts = max(1, int(self.config.result_max_attempts))
            if not self.config.result_extra_tap_enabled:
                max_attempts = 1
            next_attempt = self.result_attempts + 1
            if next_attempt <= max_attempts:
                delay = (
                    self.config.min_action_interval_s
                    if self.result_attempts == 0
                    else self.config.result_extra_tap_delay_s
                )
                if self._allowed(detection.timestamp_s, self.last_result_timestamp, delay):
                    target = detection.continue_box
                    if target is not None:
                        reason = (
                            "stable dynamically detected result continue control"
                            if next_attempt == 1
                            else f"dynamic result continue retry #{next_attempt}"
                        )
                    elif detection.result_fallback_box is not None and not self.result_extra_tap_handled:
                        # Only use the reward-content fallback when no
                        # explicit continue/dismiss control is visible. It is
                        # bounded by the same attempt limit and is never
                        # preferred over a detected control.
                        target = detection.result_fallback_box
                        self.result_extra_tap_handled = True
                        reason = f"dynamic result reward-overlay fallback #{next_attempt}"
                    else:
                        target = None
                        reason = ""
                    if target is not None:
                        self.result_attempts = next_attempt
                        self.result_handled = True
                        self.last_result_timestamp = detection.timestamp_s
                        return self._tap_for_box(detection, target, reason)
        return None

    def record_input_dispatch(self, source_mode: str, latency_s: float) -> None:
        """Keep separate moving latency estimates for scrcpy and ADB paths."""

        if not math.isfinite(latency_s) or latency_s < 0:
            return
        key = "adb" if source_mode.startswith("adb") else "scrcpy" if source_mode.startswith("scrcpy") else source_mode
        window = max(1, int(self.config.qte_latency_sample_window))
        samples = self.dispatch_latency_samples.get(key)
        if samples is None or samples.maxlen != window:
            samples = deque(samples or (), maxlen=window)
            self.dispatch_latency_samples[key] = samples
        samples.append(float(latency_s))

    def latency_estimates(self) -> dict[str, float]:
        """Return moving median dispatch latency by input transport."""

        return {
            source: round(float(median(samples)) * 1000.0, 2)
            for source, samples in self.dispatch_latency_samples.items()
            if samples
        }

    @staticmethod
    def _range_value(value: tuple[float, float] | None) -> list[float] | None:
        if value is None:
            return None
        low, high = sorted((float(value[0]), float(value[1])))
        return [round(low, 5), round(high, 5)]

    def _range_for_detection(self, detection: Detection, name: str) -> list[float] | None:
        value = getattr(detection, name, None)
        if value is None:
            feature_value = detection.features.get(name)
            if isinstance(feature_value, (tuple, list)) and len(feature_value) == 2:
                value = (float(feature_value[0]), float(feature_value[1]))
        return self._range_value(value)

    def _target_for_detection(self, detection: Detection) -> tuple[float, float] | None:
        """Read the planner-safe range while accepting legacy detections."""

        return (
            detection.gauge_safe_click_range
            or detection.gauge_tracked_target_range
            or detection.gauge_target_range
        )

    def _set_qte_diagnostics(self, detection: Detection, **values: object) -> None:
        qte = detection.features.get("qte")
        if not isinstance(qte, dict):
            qte = {}
            detection.features["qte"] = qte
        qte.update(values)
        self.last_qte_diagnostics = dict(qte)

    def _marker_in_target(
        self,
        marker: float,
        target: tuple[float, float],
        marker_width: float | None = None,
    ) -> bool:
        """Check a marker against a target using both centers and widths."""

        low, high = sorted(target)
        if high <= low:
            return False
        target_center = (low + high) / 2.0
        target_half_width = (high - low) / 2.0
        marker_half_width = max(0.0, marker_width or 0.0) / 2.0
        safe_half_width = max(0.0, target_half_width - marker_half_width)
        # A fixed margin is too permissive for a narrow target. Retain it as
        # an upper bound, scaled to the target's actual width.
        margin = min(
            max(0.0, self.config.qte_target_margin),
            max(target_half_width * 0.75, marker_half_width * 0.5),
        )
        return abs(marker - target_center) <= safe_half_width + margin

    def _predict_marker_details(self, detection: Detection) -> dict[str, object]:
        """Predict marker position when the Android input will land.

        The detector timestamp is taken immediately after a frame is read,
        while the touch arrives after analysis and ADB input dispatch.  A
        short, robust median velocity over recent observations compensates for
        that delay without using any fixed screen coordinate.
        """

        marker = detection.gauge_marker_x
        if marker is None:
            return {
                "predicted": None,
                "raw_predicted": None,
                "velocity": None,
                "velocity_jitter": 0.0,
                "velocity_stable": False,
                "horizon": 0.0,
                "dispatch_latency": 0.0,
                "boundary_mode": self._boundary_mode(),
                "reflection_applied": False,
            }
        self.qte_samples.append((detection.timestamp_s, marker))
        window = max(1, int(self.config.qte_velocity_samples))
        samples = list(self.qte_samples)[-(window + 1) :]
        velocities: list[float] = []
        for (t0, x0), (t1, x1) in zip(samples, samples[1:]):
            delta_t = t1 - t0
            if 0.01 <= delta_t <= 1.5:
                velocities.append((x1 - x0) / delta_t)
        velocity = float(median(velocities)) if velocities else None
        velocity_jitter = max(velocities) - min(velocities) if len(velocities) >= 2 else 0.0
        velocity_stable = (
            velocity is not None
            and abs(velocity) >= max(0.0, self.config.qte_min_velocity_norm_s)
            and (
                len(velocities) < 2
                or velocity_jitter <= max(
                    0.0,
                    float(getattr(self.config, "qte_velocity_max_jitter_norm_s", 0.8)),
                    abs(velocity) * 1.5,
                )
            )
        )
        dispatch_latency = self._dispatch_latency_for(detection.source_mode)
        frame_age = detection.frame_age_s if detection.frame_age_s is not None and math.isfinite(detection.frame_age_s) else 0.0
        analysis_duration = (
            detection.analysis_duration_s
            if detection.analysis_duration_s is not None and math.isfinite(detection.analysis_duration_s)
            else 0.0
        )
        horizon = (
            max(0.0, frame_age)
            + max(0.0, analysis_duration)
            + dispatch_latency
            + max(0, self.config.tap_hold_ms) / 1000.0
        )
        boundary_mode = self._boundary_mode()
        raw_predicted: float | None = None
        predicted: float | None = None
        reflection_applied = False
        if velocity is not None and abs(velocity) >= max(0.0, self.config.qte_min_velocity_norm_s):
            raw_predicted = marker + velocity * horizon
            if boundary_mode == "clip":
                predicted = float(min(1.0, max(0.0, raw_predicted)))
            elif raw_predicted < 0.0 or raw_predicted > 1.0:
                predicted = self._reflect_position(raw_predicted)
                reflection_applied = True
            else:
                predicted = float(raw_predicted)
        self._set_qte_diagnostics(
            detection,
            marker_x=round(float(marker), 5),
            velocity=round(float(velocity), 5) if velocity is not None else None,
            velocity_jitter=round(float(velocity_jitter), 5),
            velocity_stable=velocity_stable,
            input_eta=round(horizon, 5),
            dispatch_latency_s=round(dispatch_latency, 5),
            predicted_position=round(float(predicted), 5) if predicted is not None else None,
            prediction_raw_position=round(float(raw_predicted), 5) if raw_predicted is not None else None,
            boundary_mode=boundary_mode,
            reflection_applied=reflection_applied,
        )
        return {
            "predicted": predicted,
            "raw_predicted": raw_predicted,
            "velocity": velocity,
            "velocity_jitter": velocity_jitter,
            "velocity_stable": velocity_stable,
            "horizon": horizon,
            "dispatch_latency": dispatch_latency,
            "boundary_mode": boundary_mode,
            "reflection_applied": reflection_applied,
        }

    def _predict_marker(self, detection: Detection) -> tuple[float | None, float | None, float]:
        """Compatibility wrapper for callers using the original prediction API."""

        prediction = self._predict_marker_details(detection)
        return prediction["predicted"], prediction["velocity"], float(prediction["horizon"])

    def _boundary_mode(self) -> str:
        mode = str(getattr(self.config, "qte_boundary_mode", "auto") or "auto").lower()
        return mode if mode in {"auto", "reflection", "clip"} else "auto"

    @staticmethod
    def _reflect_position(position: float) -> float:
        """Fold an unbounded position into [0, 1] as a bouncing marker."""

        phase = float(position) % 2.0
        return float(phase if phase <= 1.0 else 2.0 - phase)

    def _eta_to_target(
        self,
        marker: float,
        velocity: float,
        target: tuple[float, float],
        marker_width: float | None,
        input_eta: float,
    ) -> tuple[float | None, float | None, str]:
        """Return center ETA and a width-derived timing window."""

        low, high = sorted(target)
        if high <= low or not math.isfinite(velocity) or abs(velocity) < 1e-9:
            return None, None, "invalid"
        center = (low + high) / 2.0
        target_half_width = (high - low) / 2.0
        marker_half_width = max(0.0, marker_width or 0.0) / 2.0
        safe_half_width = max(0.0, target_half_width - marker_half_width)
        margin = min(
            max(0.0, self.config.qte_target_margin),
            max(target_half_width * 0.75, marker_half_width * 0.5),
        )
        timing_window = (safe_half_width + margin) / abs(velocity)
        direct_eta = (center - marker) / velocity
        mode = self._boundary_mode()
        reflection_needed = mode == "reflection" or (
            mode == "auto"
            and (marker + velocity * max(0.0, input_eta) < 0.0 or marker + velocity * max(0.0, input_eta) > 1.0 or direct_eta < 0.0)
        )
        if reflection_needed:
            eta = self._reflected_eta(marker, velocity, center)
            return eta, timing_window, "reflection" if eta is not None else "linear"
        return direct_eta, timing_window, "linear"

    @staticmethod
    def _reflected_eta(marker: float, velocity: float, target_center: float) -> float | None:
        """Find the next time a reflected linear path reaches a target center."""

        if abs(velocity) < 1e-9:
            return None
        candidates: list[float] = []
        # The timing horizon is normally a fraction of a second.  A bounded
        # periodic search also handles a fast marker crossing both edges.
        for cycle in range(-32, 33):
            for phase in (target_center + 2.0 * cycle, 2.0 - target_center + 2.0 * cycle):
                candidate = (phase - marker) / velocity
                if candidate >= -1e-9:
                    candidates.append(max(0.0, candidate))
        return min(candidates) if candidates else None

    def _dispatch_latency_for(self, source_mode: str | None) -> float:
        key = "adb" if source_mode and source_mode.startswith("adb") else "scrcpy" if source_mode and source_mode.startswith("scrcpy") else source_mode or "unknown"
        samples = self.dispatch_latency_samples.get(key)
        if samples:
            return max(0.0, float(median(samples)))
        return max(0.0, self.config.qte_input_latency_s)

    @staticmethod
    def _allowed(timestamp: float, last: float, interval: float) -> bool:
        return last < 0 or timestamp - last >= max(0.0, interval)

    def _tap_for_box(self, detection: Detection, box, reason: str) -> Action:
        return Action(
            action_type=ActionType.TAP,
            x=box.cx,
            y=box.cy,
            x_norm=box.cx / max(1, detection.frame_width),
            y_norm=box.cy / max(1, detection.frame_height),
            hold_ms=max(0, self.config.tap_hold_ms),
            reason=reason,
            confidence=detection.confidence,
        )


@dataclass
class AutomationProgress:
    """Track full-auto completion and fail-safe stage timeouts."""

    max_rounds: int = 1
    completed_rounds: int = 0
    current_state: FishingState = FishingState.UNKNOWN
    current_phase: AutomationPhase = AutomationPhase.UNKNOWN
    phase_started_timestamp_s: float | None = None
    result_seen: bool = False
    round_active: bool = False
    start_allowed: bool = True
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")

    @property
    def state_started_timestamp_s(self) -> float | None:
        """Compatibility alias for callers that only read the timer."""

        return self.phase_started_timestamp_s

    def observe(
        self,
        state: FishingState,
        timestamp_s: float,
        transition: StateTransition | None = None,
    ) -> str | None:
        phase = self._phase_for_state(state)
        if self.phase_started_timestamp_s is None or phase != self.current_phase:
            self.current_phase = phase
            self.phase_started_timestamp_s = timestamp_s
        self.current_state = state

        if state == FishingState.RESULT:
            self.result_seen = True

        if state in {
            FishingState.PROMPT,
            FishingState.CASTING,
            FishingState.QTE,
            FishingState.QUALITY,
            FishingState.RESULT,
        }:
            self.round_active = True
            self.start_allowed = False

        if state == FishingState.WAITING and self.result_seen:
            self.completed_rounds += 1
            self.result_seen = False
            self.round_active = False
            self.start_allowed = self.completed_rounds < self.max_rounds
            if self.completed_rounds >= self.max_rounds:
                self.stop_reason = "completed_rounds"
        elif state == FishingState.WAITING and not self.round_active:
            self.start_allowed = True
        return self.stop_reason

    def timed_out(self, timestamp_s: float, config: AutomationConfig) -> bool:
        if self.stop_reason is not None or self.phase_started_timestamp_s is None:
            return False
        timeout = {
            AutomationPhase.UNKNOWN: config.unknown_timeout_s,
            AutomationPhase.WAITING: config.unconfirmed_waiting_timeout_s
            if self.round_active
            else config.waiting_timeout_s,
            AutomationPhase.PROMPT: config.prompt_timeout_s,
            AutomationPhase.CASTING: config.casting_timeout_s,
            # QTE and QUALITY are one continuous fishing phase.  Use the
            # larger existing limit so sharing the timer never shortens the
            # previous QTE allowance.
            AutomationPhase.FISHING_QTE: max(config.qte_timeout_s, config.quality_timeout_s),
            AutomationPhase.RESULT: config.result_timeout_s,
        }.get(self.current_phase)
        if timeout is None:
            return False
        return timestamp_s - self.phase_started_timestamp_s >= max(0.0, timeout)

    @staticmethod
    def _phase_for_state(state: FishingState) -> AutomationPhase:
        return {
            FishingState.UNKNOWN: AutomationPhase.UNKNOWN,
            FishingState.WAITING: AutomationPhase.WAITING,
            FishingState.PROMPT: AutomationPhase.PROMPT,
            FishingState.CASTING: AutomationPhase.CASTING,
            FishingState.QTE: AutomationPhase.FISHING_QTE,
            FishingState.QUALITY: AutomationPhase.FISHING_QTE,
            FishingState.RESULT: AutomationPhase.RESULT,
            FishingState.ERROR: AutomationPhase.ERROR,
        }[state]
