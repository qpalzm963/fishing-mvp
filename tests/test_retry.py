"""Attempt outcomes, shared budgets and fresh-screen recovery gates."""

from dataclasses import replace

import pytest

from fishing_mvp.config import AppConfig
from fishing_mvp.models import Box, Detection, FishingState
from fishing_mvp.retry import AttemptRecovery
from fishing_mvp.state_machine import AutomationProgress


def observation(state, t, **kwargs):
    return Detection(frame_index=int(t * 100) + 1, timestamp_s=t,
                     frame_width=100, frame_height=200, hint=state,
                     confidence=0.9, action_button=Box(40, 160, 20, 20), **kwargs)


def recovery(**kwargs):
    config = AppConfig()
    config.automation = replace(config.automation, auto_retry_enabled=True, **kwargs)
    config.state_machine.stable_frames = 2
    return AttemptRecovery(config, AutomationProgress(max_rounds=3))


def observe(controller, state, t, **kwargs):
    return controller.update(observation(state, t, **kwargs), state, None)


def miss(controller, t=0):
    observe(controller, FishingState.QTE, t)
    return observe(controller, FishingState.WAITING, t + 0.1)


def restart(controller, t):
    observe(controller, FishingState.WAITING, t)
    observe(controller, FishingState.WAITING, t + 0.1)


def test_disabled_keeps_unconfirmed_waiting_timeout_and_reports_qte_miss():
    controller = recovery()
    controller.config.automation.auto_retry_enabled = False
    assert not miss(controller)
    observe(controller, FishingState.WAITING, 8)
    assert controller.progress.stop_reason is None
    observe(controller, FishingState.WAITING, 8.2)
    assert controller.progress.stop_reason == "state_timeout:waiting"
    assert controller.events[-1]["failure_reason"] == "qte_miss"
    assert controller.retries == 0


def test_budget_is_shared_across_successful_rounds():
    controller = recovery(max_retry_attempts=1)
    assert miss(controller)
    restart(controller, 1.1)
    assert controller.retries == 1
    observe(controller, FishingState.RESULT, 2)
    observe(controller, FishingState.WAITING, 3)
    assert controller.progress.completed_rounds == 1
    assert controller.attempt == 3
    assert not miss(controller, 4)
    assert controller.progress.stop_reason == "max_retry_attempts"
    assert controller.events[-1]["max_retries_reached"]


@pytest.mark.parametrize("state", [FishingState.QTE, FishingState.QUALITY,
                                   FishingState.PROMPT, FishingState.CASTING,
                                   FishingState.RESULT, FishingState.WAITING])
def test_known_phase_timeout_can_wait_for_safe_recovery(state):
    controller = recovery()
    observe(controller, state, 0)
    assert observe(controller, state, 40)
    assert controller.recovering
    assert not controller.progress.start_allowed
    # Even a late RESULT during recovery cannot turn a failed attempt into a success.
    observe(controller, FishingState.RESULT, 40.1)
    restart(controller, 41.1)
    assert controller.retries == 1
    assert controller.progress.completed_rounds == 0
    assert controller.progress.start_allowed


@pytest.mark.parametrize("state", [FishingState.UNKNOWN, FishingState.ERROR])
def test_unknown_timeout_and_error_are_fatal(state):
    controller = recovery()
    observe(controller, state, 0)
    observe(controller, state, 40)
    assert not controller.recovering
    assert controller.retries == 0
    assert controller.progress.stop_reason is not None
    assert not controller.events[-1]["recoverable"]


@pytest.mark.parametrize("changes", [
    {"confidence": 0.1}, {"action_button": None},
    {"gauge_box": Box(1, 1, 30, 5)}, {"prompt_box": Box(1, 1, 30, 5)},
    {"hint": FishingState.UNKNOWN}, {"frame_reused": True},
    {"frame_decoded_s": 0.05},
])
def test_unsafe_or_stale_waiting_cannot_restart(changes):
    controller = recovery()
    miss(controller)
    for t in (1.2, 1.3, 1.4):
        detection = replace(observation(FishingState.WAITING, t), **changes)
        controller.update(detection, FishingState.WAITING, None)
    assert controller.recovering
    assert controller.retries == 0
    observe(controller, FishingState.WAITING, 8.2)
    assert controller.progress.stop_reason == "retry_recovery_timeout"


def test_post_failure_static_scrcpy_frame_can_establish_stability():
    controller = recovery()
    observe(controller, FishingState.QTE, 0, frame_pts_us=100)
    observe(controller, FishingState.WAITING, 0.1, frame_pts_us=200)
    # Old frame cannot count even if its decoded timestamp is misleading.
    observe(controller, FishingState.WAITING, 1.1, frame_pts_us=200, frame_decoded_s=1)
    assert controller.waiting_frames == 0
    observe(controller, FishingState.WAITING, 1.2, frame_pts_us=300, frame_decoded_s=1.1)
    observe(controller, FishingState.WAITING, 1.3, frame_pts_us=300,
            frame_decoded_s=1.1, frame_reused=True)
    assert not controller.recovering
    assert controller.retries == 1


def test_delay_and_consecutive_stability_are_both_required():
    controller = recovery()
    miss(controller)
    restart(controller, 0.2)
    assert controller.recovering
    observe(controller, FishingState.UNKNOWN, 1.1)
    observe(controller, FishingState.WAITING, 1.2)
    assert controller.recovering
    observe(controller, FishingState.WAITING, 1.3)
    assert not controller.recovering


def test_single_tap_miss_does_not_end_a_live_qte_phase():
    controller = recovery()
    for t in (0, 1, 2, 3):
        observe(controller, FishingState.QTE, t)
    assert not controller.events
    assert not controller.recovering
    observe(controller, FishingState.QUALITY, 4)
    observe(controller, FishingState.RESULT, 5)
    observe(controller, FishingState.WAITING, 6)
    assert controller.progress.completed_rounds == 1
    assert controller.retries == 0


def test_zero_budget_never_restarts():
    controller = recovery(max_retry_attempts=0)
    assert not miss(controller)
    assert controller.progress.stop_reason == "max_retry_attempts"


def test_unknown_timeout_still_applies_during_longer_recovery_wait():
    controller = recovery(unknown_timeout_s=1, retry_recovery_timeout_s=20)
    miss(controller)
    observe(controller, FishingState.UNKNOWN, 0.2)
    observe(controller, FishingState.UNKNOWN, 1.3)
    assert controller.progress.stop_reason == "state_timeout:unknown"
    assert controller.retries == 0
