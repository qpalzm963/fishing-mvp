"""Temporal smoothing and conservative action proposals."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from statistics import median

from .config import ActionConfig, AutomationConfig, StateMachineConfig
from .models import Action, ActionType, Detection, FishingState, StateTransition


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
    qte_samples: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=8))
    result_handled: bool = False

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
            self.qte_samples.clear()
        if state != FishingState.RESULT:
            self.result_handled = False
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
            and detection.gauge_marker_x is not None
            and detection.gauge_target_range is not None
        ):
            predicted, velocity, horizon = self._predict_marker(detection)
            in_target = self._marker_in_target(detection.gauge_marker_x, detection.gauge_target_range)
            if self.qte_in_target and in_target:
                self.qte_seen_inside = True
            elif self.qte_in_target and self.qte_seen_inside and not in_target:
                # A complete target crossing re-arms the next sweep.  When a
                # predictive tap fires before visual entry, keep the latch
                # until the marker has actually been observed inside once.
                self.qte_in_target = False
                self.qte_seen_inside = False

            if not self.qte_in_target:
                should_tap = False
                if predicted is not None:
                    should_tap = self._marker_in_target(predicted, detection.gauge_target_range)
                    reason = (
                        "predicted marker in target "
                        f"(now={detection.gauge_marker_x:.3f},pred={predicted:.3f},"
                        f"v={velocity:+.3f}/s,horizon={horizon * 1000:.0f}ms)"
                    )
                else:
                    # The first QTE frame has no velocity history.  An
                    # immediate in-range tap is safer than waiting for a
                    # second frame and missing a short target window.
                    should_tap = in_target
                    reason = "marker entered target without stable velocity estimate"
                if (
                    should_tap
                    and self._allowed(detection.timestamp_s, self.last_qte_timestamp, self.config.qte_min_interval_s)
                ):
                    self.qte_in_target = True
                    self.qte_seen_inside = in_target
                    self.last_qte_timestamp = detection.timestamp_s
                    return self._tap_for_box(detection, detection.action_button, reason)

        if (
            state == FishingState.RESULT
            and self.config.auto_continue
            and not self.result_handled
            and detection.continue_box is not None
            and self._allowed(detection.timestamp_s, self.last_result_timestamp, self.config.min_action_interval_s)
        ):
            self.result_handled = True
            self.last_result_timestamp = detection.timestamp_s
            return self._tap_for_box(detection, detection.continue_box, "stable dynamically detected result continue control")
        return None

    def _marker_in_target(self, marker: float, target: tuple[float, float]) -> bool:
        margin = max(0.0, self.config.qte_target_margin)
        return target[0] - margin <= marker <= target[1] + margin

    def _predict_marker(self, detection: Detection) -> tuple[float | None, float | None, float]:
        """Predict marker position when the Android input will land.

        The detector timestamp is taken immediately after a frame is read,
        while the touch arrives after analysis and ADB input dispatch.  A
        short, robust median velocity over recent observations compensates for
        that delay without using any fixed screen coordinate.
        """

        marker = detection.gauge_marker_x
        if marker is None:
            return None, None, 0.0
        self.qte_samples.append((detection.timestamp_s, marker))
        window = max(1, int(self.config.qte_velocity_samples))
        samples = list(self.qte_samples)[-(window + 1) :]
        velocities: list[float] = []
        for (t0, x0), (t1, x1) in zip(samples, samples[1:]):
            delta_t = t1 - t0
            if 0.01 <= delta_t <= 1.5:
                velocities.append((x1 - x0) / delta_t)
        velocity = float(median(velocities)) if velocities else None
        horizon = max(0.0, self.config.qte_input_latency_s) + max(0, self.config.tap_hold_ms) / 1000.0
        if velocity is None or abs(velocity) < max(0.0, self.config.qte_min_velocity_norm_s):
            return None, velocity, horizon
        predicted = float(min(1.0, max(0.0, marker + velocity * horizon)))
        return predicted, velocity, horizon

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
    state_started_timestamp_s: float | None = None
    result_seen: bool = False
    round_active: bool = False
    start_allowed: bool = True
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")

    def observe(
        self,
        state: FishingState,
        timestamp_s: float,
        transition: StateTransition | None = None,
    ) -> str | None:
        if self.state_started_timestamp_s is None or state != self.current_state:
            self.current_state = state
            self.state_started_timestamp_s = timestamp_s

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
        if self.stop_reason is not None or self.state_started_timestamp_s is None:
            return False
        timeout = {
            FishingState.UNKNOWN: config.unknown_timeout_s,
            FishingState.WAITING: config.unconfirmed_waiting_timeout_s if self.round_active else config.waiting_timeout_s,
            FishingState.PROMPT: config.prompt_timeout_s,
            FishingState.CASTING: config.casting_timeout_s,
            FishingState.QTE: config.qte_timeout_s,
            FishingState.QUALITY: config.quality_timeout_s,
            FishingState.RESULT: config.result_timeout_s,
        }.get(self.current_state)
        if timeout is None:
            return False
        return timestamp_s - self.state_started_timestamp_s >= max(0.0, timeout)
