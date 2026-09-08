"""Temporal smoothing and conservative action proposals."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ActionConfig, StateMachineConfig
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
    prompt_handled: bool = False
    qte_in_target: bool = False

    def plan(
        self,
        detection: Detection,
        state: FishingState,
        transition: StateTransition | None = None,
    ) -> Action | None:
        if state != FishingState.PROMPT:
            self.prompt_handled = False
        if state != FishingState.QTE:
            self.qte_in_target = False
        if detection.confidence < self.config.min_confidence:
            return None
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
            in_target = self._marker_in_target(detection.gauge_marker_x, detection.gauge_target_range)
            if not in_target:
                self.qte_in_target = False
            elif (
                not self.qte_in_target
                and self._allowed(detection.timestamp_s, self.last_qte_timestamp, self.config.qte_min_interval_s)
            ):
                self.qte_in_target = True
                self.last_qte_timestamp = detection.timestamp_s
                return self._tap_for_box(
                    detection,
                    detection.action_button,
                    "gauge marker entered detected target colour range",
                )

        if (
            state == FishingState.RESULT
            and self.config.auto_continue
            and detection.continue_box is not None
            and self._allowed(detection.timestamp_s, self.last_result_timestamp, self.config.min_action_interval_s)
        ):
            self.last_result_timestamp = detection.timestamp_s
            return self._tap_for_box(detection, detection.continue_box, "stable dynamically detected result continue control")
        return None

    def _marker_in_target(self, marker: float, target: tuple[float, float]) -> bool:
        margin = max(0.0, self.config.qte_target_margin)
        return target[0] - margin <= marker <= target[1] + margin

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
