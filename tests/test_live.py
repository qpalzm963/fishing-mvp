"""Exercise the real live loop with deterministic frames, clocks and transport."""

import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from fishing_mvp import cli, live
from fishing_mvp.actions import ADBError
from fishing_mvp.config import AppConfig
from fishing_mvp.models import Box, Detection, FishingState, FrameMetadata, StateTransition


@pytest.fixture
def session(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    events = []
    config = AppConfig()
    config.state_machine.stable_frames = 1
    config.state_machine.result_min_hold_s = 0
    frames = []
    controller = SimpleNamespace(
        assert_connected=lambda: None,
        foreground_package=lambda: "game",
        display_size=lambda: (100, 200),
        execute=lambda action: events.append("send"),
    )

    class Source:
        last_frame_metadata = None
        current = None
        closed = False

        def read(self):
            if not frames:
                raise KeyboardInterrupt
            item = frames.pop(0)
            if isinstance(item, Exception):
                raise item
            self.current = item
            self.last_frame_metadata = FrameMetadata(
                source_mode="scrcpy", frame_index=item.frame_index,
                frame_pts_us=item.frame_pts_us, reused=bool(item.frame_reused),
                decoded_at_monotonic=clock.now,
            )
            return np.zeros((item.frame_height, item.frame_width, 3), dtype=np.uint8)

        def info(self):
            return {"mode": "scrcpy"}

        def close(self):
            self.closed = True

        def send_action(self, action, device_size):
            events.append("send")
            return "scrcpy_control"

    source = Source()

    class Analyzer:
        def analyze(self, frame, frame_index, timestamp_s, **kwargs):
            return replace(source.current, frame_index=frame_index, timestamp_s=timestamp_s)

    def sleep(duration):
        clock.now += duration

    monkeypatch.setattr(live.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(live.time, "sleep", sleep)
    monkeypatch.setattr(live, "ADBController", lambda *a, **kw: controller)
    monkeypatch.setattr(live, "create_live_frame_source", lambda *a, **kw: (source, None))
    monkeypatch.setattr(live, "FrameAnalyzer", lambda config: Analyzer())
    monkeypatch.setattr(live, "scrcpy_status", lambda *a: {"available": True})
    monkeypatch.setattr(live, "draw_overlay", lambda *a: events.append("draw") or a[0])
    monkeypatch.setattr(live, "save_snapshot", lambda *a: events.append("snapshot"))
    return SimpleNamespace(config=config, source=source, frames=frames, events=events,
                           controller=controller, clock=clock)


def detection(state=FishingState.QTE, **kwargs):
    return Detection(
        frame_index=0, timestamp_s=0, frame_width=100, frame_height=200,
        hint=state, confidence=0.9, action_active=True,
        action_button=Box(40, 160, 20, 20), gauge_marker_x=0.5,
        gauge_target_range=(0.4, 0.6), **kwargs,
    )


def run(session, output, **kwargs):
    return live.run_live("phone", "game", session.config, output,
                         send_actions=True, qte_enabled=True, **kwargs)


def test_live_retry_after_narrow_qte_miss_then_success(session, tmp_path):
    session.config.automation.auto_retry_enabled = True
    session.config.automation.retry_delay_ms = 500
    session.frames.extend([
        detection(FishingState.WAITING),
        replace(detection(), gauge_target_range=(0.499, 0.501)),
        detection(FishingState.WAITING),
        *[detection(FishingState.WAITING) for _ in range(12)],
        detection(), detection(FishingState.QUALITY),
        detection(FishingState.RESULT), detection(FishingState.WAITING),
    ])
    summary = run(session, tmp_path, full_auto=True)
    assert summary["stop_reason"] == "completed_rounds"
    assert summary["completed_rounds"] == 1
    assert summary["retry"]["retry_attempts"] == 1
    events = summary["retry"]["events"]
    assert [event["decision"] for event in events] == ["wait_for_recovery", "retry_started", "success"]
    assert events[0]["failure_reason"] == "qte_miss"
    assert events[-1]["after_retry"]
    assert events[1]["timestamp_s"] - events[0]["timestamp_s"] >= 0.5
    starts = [action for action in summary["actions"] if "waiting/start" in action["reason"]]
    assert len(starts) == 2
    assert [action["attempt"] for action in starts] == [1, 2]
    assert not any(events[0]["timestamp_s"] <= action["timestamp_s"] < events[1]["timestamp_s"]
                   for action in summary["actions"])
    records = [json.loads(line) for line in (tmp_path / "live_detections.jsonl").read_text().splitlines()]
    assert all(record["action"] is None for record in records if record["recovering"])


def test_sent_start_without_transition_stops_once_with_diagnostics(session, tmp_path):
    session.config.automation.unconfirmed_waiting_timeout_s = 0.25
    session.frames.extend([detection(FishingState.WAITING) for _ in range(6)])

    summary = run(session, tmp_path, full_auto=True)

    assert summary["stop_reason"] == "start_unconfirmed"
    assert summary["completed_rounds"] == 0
    starts = [item for item in summary["actions"] if "waiting/start" in item["reason"]]
    assert len(starts) == 1
    event = summary["retry"]["events"][0]
    assert event["failure_reason"] == "start_unconfirmed"
    assert event["start_action"]["input_path"] == "scrcpy_control"
    assert event["start_action"]["x"] == starts[0]["sent_action"]["x"]
    assert event["elapsed_since_start_s"] >= 0.25


def test_unconfirmed_start_uses_bounded_retry_before_next_tap(session, tmp_path):
    session.config.automation.auto_retry_enabled = True
    session.config.automation.unconfirmed_waiting_timeout_s = 0.25
    session.config.automation.retry_delay_ms = 100
    session.frames.extend([
        *[detection(FishingState.WAITING) for _ in range(6)],
        detection(FishingState.PROMPT), detection(FishingState.CASTING),
        detection(FishingState.RESULT), detection(FishingState.WAITING),
    ])

    summary = run(session, tmp_path, full_auto=True)

    assert summary["stop_reason"] == "completed_rounds"
    assert summary["completed_rounds"] == 1
    assert summary["retry"]["retry_attempts"] == 1
    events = summary["retry"]["events"]
    assert events[0]["failure_reason"] == "start_unconfirmed"
    assert events[0]["decision"] == "wait_for_recovery"
    starts = [item for item in summary["actions"] if "waiting/start" in item["reason"]]
    assert len(starts) == 2
    assert starts[1]["timestamp_s"] - starts[0]["timestamp_s"] >= 0.35


def test_live_retry_exhausts_session_budget(session, tmp_path):
    session.config.automation.auto_retry_enabled = True
    session.config.automation.retry_delay_ms = 0
    session.frames.extend([detection(state) for _ in range(4) for state in
                           (FishingState.QTE, FishingState.WAITING, FishingState.WAITING)])
    summary = run(session, tmp_path, full_auto=True)
    assert summary["stop_reason"] == "max_retry_attempts"
    assert summary["completed_rounds"] == 0
    assert summary["retry"]["retry_attempts"] == 3
    assert summary["retry"]["attempt"] == 4
    assert len([a for a in summary["actions"] if "waiting/start" in a["reason"]]) == 3


@pytest.mark.parametrize("error", [RuntimeError("capture lost"), PermissionError("permission denied"), ADBError("disconnected")])
def test_live_retry_does_not_swallow_fatal_errors(session, tmp_path, error):
    session.config.automation.auto_retry_enabled = True
    session.frames.extend([detection(), detection(FishingState.WAITING), error])
    with pytest.raises(RuntimeError):
        run(session, tmp_path, full_auto=True)
    summary = json.loads((tmp_path / "live_summary.json").read_text())
    assert summary["stop_reason"] == "error"
    assert summary["retry"]["retry_attempts"] == 0
    assert summary["error"]["message"] == str(error)
    assert summary["retry"]["events"][-1]["decision"] == "session_stopped"
    assert session.source.closed


def test_live_stop_during_recovery_sends_no_new_input(session, tmp_path):
    session.config.automation.auto_retry_enabled = True
    session.frames.extend([detection(), detection(FishingState.WAITING), detection(FishingState.WAITING)])
    summary = run(session, tmp_path, full_auto=True,
                  stop_requested=lambda: len(session.frames) <= 1)
    assert summary["stop_reason"] == "user_stop"
    assert summary["retry"]["retry_attempts"] == 0
    assert len(summary["actions"]) == 1


def test_live_recovery_timeout_never_blindly_taps_result(session, tmp_path):
    session.config.automation.auto_retry_enabled = True
    session.config.automation.retry_recovery_timeout_s = 0.3
    session.frames.extend([detection(), detection(FishingState.WAITING),
                           *[replace(detection(FishingState.RESULT), continue_box=Box(1, 1, 20, 10)) for _ in range(10)]])
    summary = run(session, tmp_path, full_auto=True)
    assert summary["stop_reason"] == "retry_recovery_timeout"
    assert summary["completed_rounds"] == 0
    assert len(summary["actions"]) == 1


def test_live_retry_resets_temporal_runtime_and_cancels_old_observations(session, tmp_path, monkeypatch):
    session.config.automation.auto_retry_enabled = True
    session.config.automation.retry_delay_ms = 0
    session.config.automation.qte_timeout_s = 0.05
    session.config.automation.quality_timeout_s = 0.05
    analyzers, machines, planners = [], [], []
    for name, instances in (("FrameAnalyzer", analyzers), ("FishingStateMachine", machines), ("ActionPlanner", planners)):
        original = getattr(live, name)
        def factory(config, original=original, instances=instances):
            instance = original(config)
            instances.append(instance)
            return instance
        monkeypatch.setattr(live, name, factory)
    session.frames.extend([detection(), detection(), detection(),
                           detection(FishingState.WAITING), detection(),
                           detection(FishingState.RESULT), detection(FishingState.WAITING)])
    summary = run(session, tmp_path, full_auto=True)
    assert summary["stop_reason"] == "completed_rounds"
    assert len(analyzers) == len(machines) == len(planners) == 2
    qte_actions = [a for a in summary["actions"] if "target" in a["reason"]]
    assert len(qte_actions) == 2
    assert qte_actions[0]["observation_status"] == "attempt_failed"
    assert qte_actions[0].get("state_transition_observed_s") is None
    assert qte_actions[1]["attempt"] == 2


def test_retry_dry_run_never_dispatches(session, tmp_path):
    session.config.automation.auto_retry_enabled = True
    session.config.automation.retry_delay_ms = 0
    session.frames.extend([detection(), detection(FishingState.WAITING), detection(FishingState.WAITING),
                           detection(FishingState.RESULT), detection(FishingState.WAITING)])
    summary = live.run_live("phone", "game", session.config, tmp_path, full_auto=True)
    assert summary["retry"]["retry_attempts"] == 1
    assert summary["stop_reason"] == "completed_rounds"
    assert "send" not in session.events
    assert all(not action["sent"] for action in summary["actions"])


def test_retry_config_does_not_enable_automation_in_manual_live(session, tmp_path):
    session.config.automation.auto_retry_enabled = True
    session.frames.extend([detection(), detection(FishingState.WAITING)])
    summary = run(session, tmp_path)
    assert summary["retry"] is None
    assert summary["stop_reason"] == "keyboard_interrupt"


@pytest.mark.parametrize("failure_point", ["startup", "input"])
def test_auto_retry_never_reinitializes_a_failed_backend(session, tmp_path, monkeypatch, failure_point):
    session.config.automation.auto_retry_enabled = True
    session.frames.append(detection())
    calls = []
    def fail(*args, **kwargs):
        calls.append(failure_point)
        raise RuntimeError(f"{failure_point} failed")
    if failure_point == "startup":
        monkeypatch.setattr(live, "create_live_frame_source", fail)
    else:
        monkeypatch.setattr(session.source, "send_action", fail)
    with pytest.raises(RuntimeError, match=f"{failure_point} failed"):
        run(session, tmp_path, full_auto=True)
    assert calls == [failure_point]
    summary = json.loads((tmp_path / "live_summary.json").read_text())
    assert summary["stop_reason"] == "error"
    assert summary["retry"]["retry_attempts"] == 0


def test_live_sends_before_snapshot_and_survives_snapshot_failure(session, tmp_path, monkeypatch, caplog):
    session.frames.append(detection())

    def fail_snapshot(*args):
        session.events.append("snapshot")
        raise OSError("snapshot disk error")

    monkeypatch.setattr(live, "save_snapshot", fail_snapshot)
    summary = run(session, tmp_path)
    assert session.events == ["send", "draw", "snapshot"]
    assert summary["frame_count"] == 1
    assert summary["stop_reason"] == "keyboard_interrupt"
    assert summary["warning_counts"] == {"snapshot_failed": 1}
    assert "snapshot disk error" in caplog.text
    assert session.source.closed
    assert not session.config.action.qte_enabled  # caller's config is unchanged
    record = json.loads((tmp_path / "live_detections.jsonl").read_text())
    assert record["action_sent"] is True
    assert record["snapshot_error"] == "snapshot disk error"


def test_live_reused_qte_cannot_send_but_next_new_frame_can(session, tmp_path):
    session.frames.extend([
        detection(frame_reused=True, frame_pts_us=0),
        detection(frame_reused=False, frame_pts_us=100_000),
    ])
    summary = run(session, tmp_path)
    assert session.events.count("send") == 1
    assert summary["actions"][0]["loop_frame_index"] == 1


def test_live_static_waiting_frame_can_still_start(session, tmp_path):
    session.frames.append(detection(FishingState.WAITING, frame_reused=True))
    summary = run(session, tmp_path, full_auto=True)
    assert len(summary["actions"]) == 1
    assert "waiting/start" in summary["actions"][0]["reason"]


@pytest.mark.parametrize("failure", ["read", "foreground", "resize", "send", "startup", "capture_start", "close"])
def test_live_errors_save_summary_and_cli_returns_failure(session, tmp_path, monkeypatch, failure):
    session.frames.append(detection(FishingState.WAITING))
    if failure == "read":
        session.frames.append(ADBError("device disconnected"))
    elif failure == "foreground":
        session.config.automation.foreground_check_interval_s = 0
        packages = iter(["game", "game", "other"])
        session.controller.foreground_package = lambda: next(packages)
        session.frames.append(detection())
    elif failure == "resize":
        session.frames.append(replace(detection(), frame_width=200))
    elif failure == "send":
        session.frames.append(detection())
        def fail_send(*args):
            raise OSError("control disconnected")
        session.source.send_action = fail_send
    elif failure == "startup":
        def fail_start():
            raise ADBError("not connected")
        session.controller.assert_connected = fail_start
    elif failure == "capture_start":
        def fail_capture(*args, **kwargs):
            raise RuntimeError("decoder unavailable")
        monkeypatch.setattr(live, "create_live_frame_source", fail_capture)
    elif failure == "close":
        def fail_close():
            raise OSError("cleanup failed")
        session.source.close = fail_close
    # Ensure an older success summary cannot survive this failed run.
    (tmp_path / "live_summary.json").write_text('{"stop_reason":"completed_rounds"}')
    monkeypatch.setattr(cli, "_config", lambda path: session.config)
    code = cli.main(["live", "--serial", "phone", "--package", "game", "--live",
                     "--enable-qte", "--output-dir", str(tmp_path)])
    assert code == 2
    summary = json.loads((tmp_path / "live_summary.json").read_text())
    assert summary["stop_reason"] == "error"
    assert summary["error"]["message"]
    assert summary["completed_rounds"] == 0
    assert summary["last_state"] in {"waiting", "qte", "unknown"}
    if failure not in {"startup", "capture_start", "close"}:
        assert session.source.closed


def test_cleanup_and_diagnostic_errors_do_not_mask_original_error(session, tmp_path, monkeypatch):
    session.frames.append(ADBError("original failure"))
    def fail(*args):
        raise OSError("secondary failure")
    session.source.close = fail
    session.source.info = fail
    monkeypatch.setattr(live, "scrcpy_status", fail)
    with pytest.raises(ADBError, match="original failure"):
        run(session, tmp_path)
    summary = json.loads((tmp_path / "live_summary.json").read_text())
    assert summary["error"]["message"] == "original failure"
    assert summary["warning_counts"] == {
        "capture_info_failed": 1, "cleanup_failed": 1, "scrcpy_status_failed": 1,
    }


def test_live_normal_round_completion_preserved(session, tmp_path):
    session.frames.extend([detection(FishingState.RESULT), detection(FishingState.WAITING)])
    summary = run(session, tmp_path, full_auto=True)
    assert summary["completed_rounds"] == 1
    assert summary["stop_reason"] == "completed_rounds"
    assert summary["error"] is None
    assert not summary["actions"]


def pending_action():
    return {"timestamp_s": 1.0, "dispatch_end_s": 1.2, "loop_frame_index": 0, "frame_pts_us": 100}


def test_pending_qte_requires_new_post_dispatch_frame_then_removes_completed():
    action = pending_action()
    pending = [action]
    reused = replace(detection(frame_reused=True, frame_pts_us=100), frame_index=1, timestamp_s=1.3)
    assert live._observe_qte_actions(pending, reused, None, timeout_s=35) == []
    pre_dispatch = replace(reused, frame_reused=False, frame_pts_us=200, frame_decoded_s=1.1)
    assert live._observe_qte_actions(pending, pre_dispatch, None, timeout_s=35) == []
    fresh = replace(pre_dispatch, frame_decoded_s=1.3)
    events = live._observe_qte_actions(pending, fresh, None, timeout_s=35)
    assert events[0]["first_following_frame_latency_ms"] == 100
    assert len(pending) == 1
    transition = StateTransition(1.4, 2, FishingState.QTE, FishingState.RESULT, 0.9, "test")
    fresh = replace(fresh, frame_index=2, timestamp_s=1.4, frame_pts_us=300)
    events = live._observe_qte_actions(pending, fresh, transition, timeout_s=35)
    assert events[0]["observation_status"] == "completed"
    assert action["observation_status"] == "completed"
    assert pending == []


def test_pending_qte_expires_and_cannot_attach_to_later_round():
    action = pending_action()
    pending = [action]
    late = replace(detection(), frame_index=5, timestamp_s=37)
    events = live._observe_qte_actions(pending, late, None, timeout_s=35)
    assert events[0]["observation_status"] == "timed_out"
    assert pending == []
    assert action["observation_status"] == "timed_out"


def test_pending_qte_list_stays_bounded_over_many_rounds():
    pending = []
    for index in range(1000):
        pending.append({"timestamp_s": float(index), "loop_frame_index": index * 2})
        frame = replace(detection(), frame_index=index * 2 + 1, timestamp_s=index + 0.1)
        transition = StateTransition(frame.timestamp_s, frame.frame_index, FishingState.QTE,
                                     FishingState.RESULT, 0.9, "test")
        live._observe_qte_actions(pending, frame, transition, timeout_s=35)
        assert pending == []


def test_stop_requested_before_capture_saves_summary_without_input(session, tmp_path):
    session.frames.append(detection())
    summary = run(session, tmp_path, stop_requested=lambda: True)
    assert summary["stop_reason"] == "user_stop"
    assert summary["frame_count"] == 0
    assert "send" not in session.events
    assert session.source.closed


def test_stop_arriving_during_analysis_prevents_action(session, tmp_path):
    session.frames.append(detection())
    checks = iter([False, True])
    summary = run(session, tmp_path, stop_requested=lambda: next(checks))
    assert summary["stop_reason"] == "user_stop"
    assert "send" not in session.events
    assert session.source.closed


def test_live_progress_reports_round_completion(session, tmp_path):
    session.frames.extend([detection(FishingState.RESULT), detection(FishingState.WAITING)])
    progress = []
    run(session, tmp_path, full_auto=True, on_progress=lambda *args: progress.append(args))
    assert progress[-1][:3] == (FishingState.WAITING, 1, 1)
