from fishing_mvp.config import ActionConfig, StateMachineConfig
from fishing_mvp.models import Box, Detection, FishingState, StateTransition
from fishing_mvp.state_machine import ActionPlanner, FishingStateMachine


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


def test_qte_actions_are_opt_in():
    machine = FishingStateMachine(StateMachineConfig(stable_frames=1))
    disabled = ActionPlanner(ActionConfig(min_confidence=0.5, qte_enabled=False))
    state, transition = machine.update(make_detection(0, FishingState.QTE))
    assert disabled.plan(make_detection(0, FishingState.QTE), state, transition) is None
    enabled = ActionPlanner(ActionConfig(min_confidence=0.5, qte_enabled=True, qte_min_interval_s=0.1))
    action = enabled.plan(make_detection(0, FishingState.QTE), state, transition)
    assert action is not None and action.action_type.value == "tap"


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
