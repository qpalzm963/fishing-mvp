from fishing_mvp.config import ActionConfig, AutomationConfig, StateMachineConfig
from fishing_mvp.models import Box, Detection, FishingState, StateTransition
from fishing_mvp.state_machine import ActionPlanner, AutomationProgress, FishingStateMachine


def make_detection(index: int, hint: FishingState, confidence: float = 0.9) -> Detection:
    return Detection(
        frame_index=index,
        timestamp_s=index / 10,
        frame_width=1000,
        frame_height=2000,
        action_button=Box(450, 1600, 100, 100),
        action_active=hint in {FishingState.PROMPT, FishingState.QTE},
        prompt_box=Box(430, 1450, 140, 50) if hint == FishingState.PROMPT else None,
        gauge_box=Box(360, 1450, 280, 45) if hint in {FishingState.QTE, FishingState.QUALITY} else None,
        gauge_marker_x=0.5 if hint in {FishingState.QTE, FishingState.QUALITY} else None,
        gauge_target_range=(0.35, 0.65) if hint in {FishingState.QTE, FishingState.QUALITY} else None,
        quality="great" if hint == FishingState.QUALITY else None,
        result_box=Box(0, 0, 1000, 2000) if hint == FishingState.RESULT else None,
        hint=hint,
        confidence=confidence,
    )


def test_state_machine_requires_stable_frames_and_graces_unknown():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=2, unknown_grace_frames=2))
    state, event = machine.update(make_detection(0, FishingState.WAITING))
    assert state == FishingState.UNKNOWN
    assert event is None
    state, event = machine.update(make_detection(1, FishingState.WAITING))
    assert state == FishingState.WAITING
    assert event is not None and event.to_state == FishingState.WAITING
    state, event = machine.update(make_detection(2, FishingState.UNKNOWN))
    assert state == FishingState.WAITING
    assert event is None


def test_prompt_action_is_derived_from_button_center_and_cooldown():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1))
    planner = ActionPlanner(ActionConfig(min_confidence=0.5, tap_hold_ms=80))
    state, transition = machine.update(make_detection(0, FishingState.PROMPT))
    action = planner.plan(make_detection(0, FishingState.PROMPT), state, transition)
    assert action is not None
    assert action.x == 500 and action.y == 1650
    assert action.x_norm == 0.5 and action.y_norm == 0.825
    state, transition = machine.update(make_detection(1, FishingState.PROMPT))
    assert planner.plan(make_detection(1, FishingState.PROMPT), state, transition) is None


def test_prompt_action_retries_until_a_confident_frame_then_handles_session():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1))
    planner = ActionPlanner(ActionConfig(min_confidence=0.68, tap_hold_ms=80))

    state, transition = machine.update(make_detection(0, FishingState.PROMPT, confidence=0.40))
    assert planner.plan(make_detection(0, FishingState.PROMPT, confidence=0.40), state, transition) is None

    state, transition = machine.update(make_detection(1, FishingState.PROMPT, confidence=0.90))
    action = planner.plan(make_detection(1, FishingState.PROMPT, confidence=0.90), state, transition)
    assert action is not None

    state, transition = machine.update(make_detection(2, FishingState.PROMPT, confidence=0.95))
    assert planner.plan(make_detection(2, FishingState.PROMPT, confidence=0.95), state, transition) is None

    state, transition = machine.update(make_detection(6, FishingState.WAITING))
    assert planner.plan(make_detection(6, FishingState.WAITING), state, transition) is None
    state, transition = machine.update(make_detection(7, FishingState.PROMPT, confidence=0.90))
    assert planner.plan(make_detection(7, FishingState.PROMPT, confidence=0.90), state, transition) is not None


def test_full_auto_starts_from_an_inactive_waiting_control_once_per_entry():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1))
    planner = ActionPlanner(ActionConfig(min_confidence=0.5, auto_start=True, min_action_interval_s=0.45))

    waiting = make_detection(0, FishingState.WAITING)
    waiting.action_active = True  # Some devices render the waiting control as saturated green.
    state, transition = machine.update(waiting)
    action = planner.plan(waiting, state, transition)
    assert action is not None
    assert action.reason == "stable dynamically detected waiting/start control"
    assert (action.x, action.y) == (500, 1650)

    same_waiting = make_detection(1, FishingState.WAITING)
    same_waiting.action_active = True
    state, transition = machine.update(same_waiting)
    assert planner.plan(same_waiting, state, transition) is None

    prompt = make_detection(2, FishingState.PROMPT)
    state, transition = machine.update(prompt)
    assert planner.plan(prompt, state, transition) is None
    later_waiting = make_detection(6, FishingState.WAITING)
    later_waiting.action_active = True
    state, transition = machine.update(later_waiting)
    assert planner.plan(later_waiting, state, transition) is not None


def test_full_auto_does_not_start_again_after_a_round_without_result():
    planner = ActionPlanner(ActionConfig(min_confidence=0.5, auto_start=True))
    detection = make_detection(0, FishingState.WAITING)
    assert planner.plan(detection, FishingState.WAITING, start_allowed=False) is None


def test_qte_actions_are_opt_in():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1))
    disabled = ActionPlanner(ActionConfig(min_confidence=0.5, qte_enabled=False))
    state, transition = machine.update(make_detection(0, FishingState.QTE))
    assert disabled.plan(make_detection(0, FishingState.QTE), state, transition) is None
    enabled = ActionPlanner(ActionConfig(min_confidence=0.5, qte_enabled=True, qte_min_interval_s=0.1))
    action = enabled.plan(make_detection(0, FishingState.QTE), state, transition)
    assert action is not None and action.action_type.value == "tap"


def test_qte_prediction_triggers_before_marker_enters_target():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1))
    planner = ActionPlanner(
        ActionConfig(
            min_confidence=0.5,
            qte_enabled=True,
            qte_min_interval_s=0.1,
            qte_target_margin=0.0,
            qte_input_latency_s=0.2,
            qte_velocity_samples=2,
            qte_min_velocity_norm_s=0.1,
        )
    )

    def qte(index: int, timestamp: float, marker: float) -> Detection:
        detection = make_detection(index, FishingState.QTE)
        detection.timestamp_s = timestamp
        detection.gauge_marker_x = marker
        detection.gauge_target_range = (0.30, 0.51)
        return detection

    first = qte(0, 0.0, 0.86)
    state, transition = machine.update(first)
    assert planner.plan(first, state, transition) is None

    second = qte(1, 0.1, 0.80)
    state, transition = machine.update(second)
    assert planner.plan(second, state, transition) is None

    third = qte(2, 0.2, 0.68)
    state, transition = machine.update(third)
    action = planner.plan(third, state, transition)
    assert action is not None
    assert "predicted marker" in action.reason
    assert "now=0.680" in action.reason


def test_qte_action_triggers_once_per_target_entry():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1))
    planner = ActionPlanner(ActionConfig(min_confidence=0.5, qte_enabled=True, qte_min_interval_s=0.18))

    state, transition = machine.update(make_detection(0, FishingState.QTE))
    assert planner.plan(make_detection(0, FishingState.QTE), state, transition) is not None

    state, transition = machine.update(make_detection(1, FishingState.QTE))
    assert planner.plan(make_detection(1, FishingState.QTE), state, transition) is None

    outside = make_detection(2, FishingState.QTE)
    outside.gauge_marker_x = 0.1
    state, transition = machine.update(outside)
    assert planner.plan(outside, state, transition) is None

    state, transition = machine.update(make_detection(3, FishingState.QTE))
    assert planner.plan(make_detection(3, FishingState.QTE), state, transition) is not None


def test_result_continue_action_is_once_per_result_entry():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1))
    planner = ActionPlanner(ActionConfig(min_confidence=0.5, auto_continue=True))
    result = make_detection(0, FishingState.RESULT)
    result.continue_box = Box(100, 1500, 800, 100)
    state, transition = machine.update(result)
    assert planner.plan(result, state, transition) is not None

    same_result = make_detection(1, FishingState.RESULT)
    same_result.continue_box = result.continue_box
    state, transition = machine.update(same_result)
    assert planner.plan(same_result, state, transition) is None


def test_result_extra_tap_is_delayed_and_once_per_result_entry():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1))
    planner = ActionPlanner(
        ActionConfig(
            min_confidence=0.5,
            auto_continue=True,
            result_extra_tap_delay_s=0.8,
        )
    )
    result = make_detection(0, FishingState.RESULT)
    result.continue_box = Box(100, 1500, 800, 100)
    result.result_fallback_box = Box(450, 800, 100, 100)
    state, transition = machine.update(result)
    first = planner.plan(result, state, transition)
    assert first is not None
    assert "continue control" in first.reason

    before_delay = make_detection(5, FishingState.RESULT)
    before_delay.timestamp_s = 0.5
    before_delay.continue_box = result.continue_box
    before_delay.result_fallback_box = result.result_fallback_box
    state, transition = machine.update(before_delay)
    assert planner.plan(before_delay, state, transition) is None

    after_delay = make_detection(9, FishingState.RESULT)
    after_delay.timestamp_s = 0.9
    after_delay.continue_box = result.continue_box
    after_delay.result_fallback_box = result.result_fallback_box
    state, transition = machine.update(after_delay)
    extra = planner.plan(after_delay, state, transition)
    assert extra is not None
    assert extra.reason == "one-time dynamic result reward-overlay dismissal tap"
    assert (extra.x, extra.y) == (500, 850)

    later = make_detection(10, FishingState.RESULT)
    later.timestamp_s = 1.1
    later.continue_box = result.continue_box
    later.result_fallback_box = result.result_fallback_box
    state, transition = machine.update(later)
    assert planner.plan(later, state, transition) is None


def test_automation_progress_counts_only_result_to_waiting_as_a_completed_round():
    progress = AutomationProgress(max_rounds=1)
    progress.observe(FishingState.WAITING, 0.0)
    progress.observe(FishingState.RESULT, 3.0)
    assert progress.completed_rounds == 0

    transition = StateTransition(
        timestamp_s=5.0,
        frame_index=50,
        from_state=FishingState.RESULT,
        to_state=FishingState.WAITING,
        confidence=0.9,
        reason="result complete",
    )
    assert progress.observe(FishingState.WAITING, 5.0, transition) == "completed_rounds"
    assert progress.completed_rounds == 1


def test_automation_progress_accepts_a_short_animation_between_result_and_waiting():
    progress = AutomationProgress(max_rounds=1)
    progress.observe(FishingState.RESULT, 1.0)
    progress.observe(FishingState.CASTING, 2.0)
    assert progress.observe(FishingState.WAITING, 3.0) == "completed_rounds"
    assert progress.completed_rounds == 1


def test_automation_progress_blocks_restart_when_an_active_round_returns_to_waiting():
    progress = AutomationProgress(max_rounds=1)
    progress.observe(FishingState.PROMPT, 1.0)
    progress.observe(FishingState.WAITING, 2.0)
    assert not progress.start_allowed
    assert progress.completed_rounds == 0
    assert progress.timed_out(10.0, AutomationConfig(unconfirmed_waiting_timeout_s=8.0))


def test_automation_progress_has_a_qte_timeout():
    progress = AutomationProgress(max_rounds=1)
    progress.observe(FishingState.QTE, 2.0)
    assert not progress.timed_out(4.9, AutomationConfig(qte_timeout_s=3.0))
    assert progress.timed_out(5.0, AutomationConfig(qte_timeout_s=3.0))


def test_qte_context_does_not_flicker_to_casting_on_motion_gap():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1))
    machine.update(make_detection(0, FishingState.QTE))
    state, transition = machine.update(make_detection(1, FishingState.CASTING))
    assert state == FishingState.QTE
    assert transition is None


def test_result_is_held_for_configured_animation_window():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1, result_min_hold_s=2.0))
    machine.update(make_detection(0, FishingState.RESULT))
    state, transition = machine.update(make_detection(10, FishingState.WAITING))
    assert state == FishingState.RESULT
    assert transition is None
    state, transition = machine.update(make_detection(25, FishingState.WAITING))
    assert state == FishingState.WAITING
    assert transition is not None
