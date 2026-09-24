"""Bounded attempt recovery for the full-auto live session."""

from dataclasses import dataclass, field
from typing import Any

from .config import AppConfig
from .models import Action, Detection, FishingState, StateTransition
from .state_machine import AutomationPhase, AutomationProgress


@dataclass
class AttemptRecovery:
    config: AppConfig
    progress: AutomationProgress
    attempt: int = 1
    retries: int = 0
    qte_seen: bool = False
    recovering_since: float | None = None
    failure_frame: int = -1
    failure_pts: int | None = None
    waiting_frames: int = 0
    unknown_since: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    last_failure: str | None = None
    start_action: dict[str, Any] | None = None

    @property
    def recovering(self) -> bool:
        return self.recovering_since is not None

    def _event(self, detection: Detection, state: FishingState, decision: str, **details: Any) -> None:
        self.events.append({
            "timestamp_s": detection.timestamp_s,
            "attempt": self.attempt,
            "retry_attempts": self.retries,
            "last_state": state.value,
            "enabled": self.config.automation.auto_retry_enabled,
            "retry_delay_ms": self.config.automation.retry_delay_ms,
            "decision": decision,
            **details,
        })

    def record_start_action(
        self, detection: Detection, action: Action, input_path: str, dispatched_at_s: float,
    ) -> None:
        """Track an actual start input until an active fishing state confirms it."""
        self.start_action = {
            "timestamp_s": dispatched_at_s,
            "x": action.x,
            "y": action.y,
            "x_norm": action.x_norm,
            "y_norm": action.y_norm,
            "input_path": input_path,
            "frame_index": detection.frame_index,
            "frame_pts_us": detection.frame_pts_us,
        }
        self.progress.start_allowed = False

    def update(self, detection: Detection, state: FishingState,
               transition: StateTransition | None) -> bool:
        """Advance progress; return True when all temporal runtime needs clearing.

        Recovery never observes round completion or permits planner input. Once
        a fresh, stable start screen is accepted, normal planning may resume.
        """
        cfg = self.config.automation
        progress = self.progress
        now = detection.timestamp_s
        if progress.stop_reason is not None:
            return False
        if self.recovering:
            assert self.recovering_since is not None
            elapsed = now - self.recovering_since
            if detection.hint == FishingState.UNKNOWN:
                if self.unknown_since is None:
                    self.unknown_since = now
            else:
                self.unknown_since = None
            if state == FishingState.ERROR or detection.hint == FishingState.ERROR:
                progress.stop_reason = "state_error"
            elif self.unknown_since is not None and now - self.unknown_since >= cfg.unknown_timeout_s:
                progress.stop_reason = "state_timeout:unknown"
            elif elapsed >= cfg.retry_recovery_timeout_s:
                progress.stop_reason = "retry_recovery_timeout"
            if progress.stop_reason:
                self._event(detection, state, "stop", failure_reason=self.last_failure,
                            stop_reason=progress.stop_reason)
                return False
            # A reused scrcpy frame is allowed only if it was captured after
            # recovery began. Static screens do not necessarily emit new PTS.
            fresh = detection.frame_index > self.failure_frame
            if detection.frame_decoded_s is not None:
                fresh = fresh and detection.frame_decoded_s > self.recovering_since
            else:
                fresh = fresh and not detection.frame_reused
            if self.failure_pts is not None and detection.frame_pts_us is not None:
                fresh = fresh and detection.frame_pts_us != self.failure_pts
            ready = (
                fresh and detection.hint == FishingState.WAITING
                and detection.confidence >= self.config.action.min_confidence
                and detection.action_button is not None
                and detection.prompt_box is None and detection.gauge_box is None
            )
            self.waiting_frames = self.waiting_frames + 1 if ready else 0
            if (state == FishingState.WAITING
                    and self.waiting_frames >= self.config.state_machine.stable_frames
                    and elapsed >= cfg.retry_delay_ms / 1000.0):
                self.retries += 1
                self.attempt += 1
                self.qte_seen = False
                self.recovering_since = None
                progress.round_active = False
                progress.result_seen = False
                progress.start_allowed = True
                progress.current_state = state
                progress.current_phase = AutomationPhase.WAITING
                progress.phase_started_timestamp_s = now
                self._event(detection, state, "retry_started", failure_reason=self.last_failure)
            return False

        before = progress.completed_rounds
        progress.observe(state, now, transition)
        if state in {FishingState.PROMPT, FishingState.CASTING, FishingState.QTE,
                     FishingState.QUALITY, FishingState.RESULT}:
            self.start_action = None
        elif self.start_action is not None:
            progress.start_allowed = False
        self.qte_seen |= state in {FishingState.QTE, FishingState.QUALITY}
        if progress.completed_rounds > before:
            self._event(detection, state, "success", after_retry=self.last_failure is not None)
            self.qte_seen = False
            self.last_failure = None
            if progress.stop_reason is None:
                self.attempt += 1
            return False

        incomplete = state == FishingState.WAITING and progress.round_active and not progress.result_seen
        timed_out = progress.timed_out(now, cfg)
        start_unconfirmed = (
            self.start_action is not None
            and state == FishingState.WAITING
            and now - self.start_action["timestamp_s"] >= max(0.0, cfg.unconfirmed_waiting_timeout_s)
        )
        state_error = cfg.auto_retry_enabled and (state == FishingState.ERROR or detection.hint == FishingState.ERROR)
        if not (start_unconfirmed or timed_out or state_error or (cfg.auto_retry_enabled and incomplete)):
            return False
        if state_error:
            stop_reason = "state_error"
        elif start_unconfirmed:
            stop_reason = "start_unconfirmed"
        else:
            stop_reason = f"state_timeout:{state.value}"
        qte_miss = self.qte_seen and (incomplete or state in {FishingState.QTE, FishingState.QUALITY})
        reason = "start_unconfirmed" if start_unconfirmed else "qte_miss" if qte_miss else "attempt_incomplete" if incomplete else stop_reason
        self.last_failure = reason
        recoverable = state not in {FishingState.UNKNOWN, FishingState.ERROR} and not state_error
        if start_unconfirmed:
            recoverable = recoverable and (
                detection.hint == FishingState.WAITING
                and detection.confidence >= self.config.action.min_confidence
                and detection.action_button is not None
                and detection.prompt_box is None and detection.gauge_box is None
            )
        exhausted = self.retries >= cfg.max_retry_attempts
        retry = cfg.auto_retry_enabled and recoverable and not exhausted
        start_details: dict[str, Any] = {}
        if start_unconfirmed:
            assert self.start_action is not None
            start_details = {
                "start_action": self.start_action,
                "elapsed_since_start_s": round(now - self.start_action["timestamp_s"], 3),
                "last_hint": detection.hint.value,
                "last_confidence": detection.confidence,
            }
        self._event(detection, state, "wait_for_recovery" if retry else "stop",
                    failure_reason=reason, recoverable=recoverable,
                    qte_miss_reason=("returned_to_waiting_without_result" if incomplete else "qte_timeout") if qte_miss else None,
                    max_retries_reached=exhausted,
                    next_attempt=self.attempt + 1 if retry else None,
                    **start_details)
        self.start_action = None
        if not retry:
            progress.stop_reason = "max_retry_attempts" if cfg.auto_retry_enabled and recoverable and exhausted else stop_reason
            self.events[-1]["stop_reason"] = progress.stop_reason
            return False
        self.recovering_since = now
        self.failure_frame = detection.frame_index
        self.failure_pts = detection.frame_pts_us
        self.waiting_frames = 0
        self.unknown_since = None
        progress.start_allowed = False
        return True

    def summary(self) -> dict[str, Any]:
        return {"enabled": self.config.automation.auto_retry_enabled,
                "attempt": self.attempt, "retry_attempts": self.retries,
                "max_retry_attempts": self.config.automation.max_retry_attempts,
                "last_failure": self.last_failure, "events": self.events}
