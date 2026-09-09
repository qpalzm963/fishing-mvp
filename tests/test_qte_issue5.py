from fishing_mvp.config import ActionConfig, DetectorConfig
from fishing_mvp.models import Box, Detection, FishingState
from fishing_mvp.state_machine import ActionPlanner
from fishing_mvp.vision import FrameAnalyzer


def make_qte_detection(
    index: int,
    timestamp: float,
    marker: float,
    target: tuple[float, float] = (0.45, 0.55),
    marker_width: float | None = None,
) -> Detection:
    return Detection(
        frame_index=index,
        timestamp_s=timestamp,
        frame_width=1000,
        frame_height=2000,
        action_button=Box(450, 1600, 100, 100),
        action_active=True,
        gauge_box=Box(360, 1450, 280, 45),
        gauge_marker_x=marker,
        gauge_marker_width=marker_width,
        gauge_target_range=target,
        gauge_raw_target_range=target,
        gauge_tracked_target_range=target,
        gauge_safe_click_range=target,
        hint=FishingState.QTE,
        confidence=0.95,
    )


def test_eta_planner_handles_left_to_right_and_right_to_left_crossovers():
    config = ActionConfig(
        min_confidence=0.5,
        qte_enabled=True,
        qte_eta_enabled=True,
        qte_min_interval_s=0.1,
        qte_target_margin=0.0,
        qte_input_latency_s=0.2,
        qte_velocity_samples=1,
        qte_min_velocity_norm_s=0.1,
    )

    for first_marker, second_marker in ((0.20, 0.30), (0.80, 0.70)):
        planner = ActionPlanner(config)
        first = make_qte_detection(0, 0.0, first_marker)
        assert planner.plan(first, FishingState.QTE) is None
        second = make_qte_detection(1, 0.1, second_marker)
        action = planner.plan(second, FishingState.QTE)

        assert action is not None
        assert action.reason.startswith("ETA predicted marker in target")
        assert second.features["qte"]["decision"] == "eta_window"
        assert second.features["qte"]["eta_match"] is True


def test_eta_timing_window_shrinks_with_target_width_and_marker_width():
    planner = ActionPlanner(ActionConfig(qte_target_margin=0.0))
    _, wide_window, _ = planner._eta_to_target(0.20, 1.0, (0.40, 0.60), None, 0.2)
    _, narrow_window, _ = planner._eta_to_target(0.20, 1.0, (0.49, 0.51), 0.01, 0.2)

    assert wide_window is not None and narrow_window is not None
    assert 0.0 < narrow_window < wide_window


def test_zero_or_noisy_velocity_falls_back_to_current_or_predicted_position():
    zero_planner = ActionPlanner(
        ActionConfig(min_confidence=0.5, qte_enabled=True, qte_target_margin=0.0, qte_min_velocity_norm_s=0.1)
    )
    first = make_qte_detection(0, 0.0, 0.75)
    second = make_qte_detection(1, 0.1, 0.75)
    assert zero_planner.plan(first, FishingState.QTE) is None
    assert zero_planner.plan(second, FishingState.QTE) is None
    assert second.features["qte"]["velocity_stable"] is False
    assert second.features["qte"]["decision"] == "outside_target"

    noisy_planner = ActionPlanner(
        ActionConfig(
            min_confidence=0.5,
            qte_enabled=True,
            qte_target_margin=0.0,
            qte_eta_enabled=True,
            qte_input_latency_s=0.2,
            qte_velocity_samples=2,
            qte_velocity_max_jitter_norm_s=0.2,
        )
    )
    assert noisy_planner.plan(make_qte_detection(0, 0.0, 0.10), FishingState.QTE) is None
    assert noisy_planner.plan(make_qte_detection(1, 0.1, 0.50), FishingState.QTE) is None
    third = make_qte_detection(2, 0.2, 0.30)
    action = noisy_planner.plan(third, FishingState.QTE)

    assert action is not None
    assert "predicted marker" in action.reason
    assert third.features["qte"]["velocity_stable"] is False
    assert third.features["qte"]["decision"] == "predicted_position"


def test_reflection_prediction_handles_edge_bounce_and_clip_mode_is_available():
    reflecting = ActionPlanner(
        ActionConfig(
            qte_input_latency_s=0.2,
            qte_velocity_samples=1,
            qte_min_velocity_norm_s=0.1,
            qte_boundary_mode="auto",
        )
    )
    reflecting._predict_marker(make_qte_detection(0, 0.0, 0.90))
    predicted, velocity, _ = reflecting._predict_marker(make_qte_detection(1, 0.1, 0.99))

    assert velocity is not None
    assert predicted is not None and round(predicted, 3) == 0.83

    clipping = ActionPlanner(
        ActionConfig(
            qte_input_latency_s=0.2,
            qte_velocity_samples=1,
            qte_min_velocity_norm_s=0.1,
            qte_boundary_mode="clip",
        )
    )
    clipping._predict_marker(make_qte_detection(0, 0.0, 0.90))
    clipped, _, _ = clipping._predict_marker(make_qte_detection(1, 0.1, 0.99))
    assert clipped == 1.0

    eta, _, mode = reflecting._eta_to_target(0.95, 0.5, (0.80, 0.90), None, 0.1)
    assert eta is not None and round(eta, 3) == 0.4
    assert mode == "reflection"


def test_planner_uses_safe_click_range_instead_of_legacy_wide_alias():
    planner = ActionPlanner(ActionConfig(min_confidence=0.5, qte_enabled=True, qte_target_margin=0.0))
    detection = make_qte_detection(0, 0.0, 0.20, target=(0.10, 0.90))
    detection.gauge_safe_click_range = (0.45, 0.55)

    assert planner.plan(detection, FishingState.QTE) is None
    assert detection.features["qte"]["safe_click_range"] == [0.45, 0.55]
    payload = detection.to_dict()
    assert payload["gauge_raw_target_range"] == [0.1, 0.9]
    assert payload["gauge_tracked_target_range"] == [0.1, 0.9]
    assert payload["gauge_safe_click_range"] == [0.45, 0.55]


def test_replay_sequence_shrinks_without_stale_widening_and_recovers_occlusion():
    analyzer = FrameAnalyzer(
        DetectorConfig(
            gauge_target_tracking_frames=4,
            gauge_target_tracking_missing_frames=2,
            gauge_target_tracking_max_width_ratio=0.18,
        )
    )
    gauge = Box(100, 500, 300, 40)
    replay = [
        ((0.58, 0.72), 0.20),
        ((0.61, 0.69), 0.38),
        ((0.64, 0.68), 0.66),
        (None, 0.66),
        ((0.645, 0.675), 0.70),
    ]
    tracked = [analyzer._stabilize_target_range(gauge, target, 0.04, marker_x=marker) for target, marker in replay]

    assert tracked[0] == (0.58, 0.72)
    assert tracked[1] == (0.61, 0.69)
    assert tracked[2] == (0.64, 0.68)
    assert tracked[3] == tracked[2]
    assert tracked[4] == (0.645, 0.675)
