"""On-device (Android) session bridge: same detector, host-fed frames."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from fishing_mvp.mobile import MobileSession, create_session, frame_from_rgba


FIXTURES = Path(__file__).parent / "fixtures"
PACKAGE = "com.example.fishing"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingDispatcher:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[float, float, int]] = []
        self.fail = fail

    def dispatchTap(self, x_norm: float, y_norm: float, hold_ms: int) -> str:  # noqa: N802
        if self.fail:
            raise RuntimeError("gesture cancelled")
        self.calls.append((x_norm, y_norm, hold_ms))
        return "accessibility_gesture"


def _fixture(name: str) -> np.ndarray:
    frame = cv2.imread(str(FIXTURES / name))
    assert frame is not None
    return frame


def _rgba_bytes(frame: np.ndarray, padding: int = 0) -> tuple[bytes, int]:
    rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
    height, width = rgba.shape[:2]
    stride = width * 4 + padding
    padded = np.zeros((height, stride), dtype=np.uint8)
    padded[:, : width * 4] = rgba.reshape(height, width * 4)
    return padded.tobytes(), stride


def test_frame_from_rgba_handles_row_padding() -> None:
    frame = _fixture("waiting.jpg")[:200, :150]
    data, stride = _rgba_bytes(frame, padding=64)
    restored = frame_from_rgba(data, frame.shape[1], frame.shape[0], stride)
    assert restored.shape == frame.shape
    assert np.array_equal(restored, frame)


def test_frame_from_rgba_rejects_short_buffer() -> None:
    with pytest.raises(ValueError):
        frame_from_rgba(b"\x00" * 10, 4, 4, 16)


def _feed(session: MobileSession, clock: FakeClock, frame: np.ndarray, count: int, package: str = PACKAGE) -> list[dict]:
    results = []
    for _ in range(count):
        clock.advance(0.1)
        results.append(session.process_frame(frame, captured_at_monotonic=clock.now - 0.01, foreground_package=package))
    return results


def test_dry_run_proposes_start_without_dispatching(tmp_path: Path) -> None:
    clock = FakeClock()
    dispatcher = RecordingDispatcher()
    session = MobileSession(tmp_path, package=PACKAGE, dispatcher=dispatcher, clock=clock)
    results = _feed(session, clock, _fixture("waiting.jpg"), 6)
    assert results[-1]["state"] == "waiting"
    proposals = [item for item in results if item["action"]]
    assert proposals, "waiting screen should yield a start proposal in full-auto"
    assert all(not item["sent"] for item in results)
    assert dispatcher.calls == []
    summary = json.loads(session.stop())
    assert summary["send_actions"] is False
    assert summary["stop_reason"] == "user_stop"
    assert (tmp_path / "live_summary.json").exists()
    assert (tmp_path / "live_detections.jsonl").read_text(encoding="utf-8").count("\n") == 6


def test_live_dispatches_normalized_tap(tmp_path: Path) -> None:
    clock = FakeClock()
    dispatcher = RecordingDispatcher()
    session = MobileSession(tmp_path, package=PACKAGE, send_actions=True, dispatcher=dispatcher, clock=clock)
    frame = _fixture("waiting.jpg")
    session.set_device_size(frame.shape[1] * 2, frame.shape[0] * 2)
    results = _feed(session, clock, frame, 6)
    assert dispatcher.calls, "live session should dispatch the start tap"
    x_norm, y_norm, hold_ms = dispatcher.calls[0]
    assert 0.0 < x_norm < 1.0 and 0.5 < y_norm < 1.0
    assert hold_ms >= 0
    sent = [item for item in results if item["sent"]]
    assert len(sent) == len(dispatcher.calls)
    summary = json.loads(session.stop())
    assert summary["input_path_counts"] == {"accessibility_gesture": len(dispatcher.calls)}
    assert summary["actions"][0]["sent_action"]["x"] == round(x_norm * frame.shape[1] * 2)


def test_foreground_mismatch_stops_before_input(tmp_path: Path) -> None:
    clock = FakeClock()
    dispatcher = RecordingDispatcher()
    session = MobileSession(tmp_path, package=PACKAGE, send_actions=True, dispatcher=dispatcher, clock=clock)
    frame = _fixture("waiting.jpg")
    session.set_device_size(frame.shape[1], frame.shape[0])
    results = _feed(session, clock, frame, 3, package="com.android.launcher")
    assert results[0]["stopped"] and results[0]["stop_reason"] == "error"
    assert "Foreground" in results[0]["error"]["message"]
    assert dispatcher.calls == []
    assert session.frame_index == 0


def test_dispatch_failure_and_size_change_stop(tmp_path: Path) -> None:
    clock = FakeClock()
    failing = MobileSession(tmp_path / "a", package=PACKAGE, send_actions=True, dispatcher=RecordingDispatcher(fail=True), clock=clock)
    frame = _fixture("waiting.jpg")
    failing.set_device_size(frame.shape[1], frame.shape[0])
    results = _feed(failing, clock, frame, 6)
    assert results[-1]["stopped"]
    assert "Input dispatch failed" in results[-1]["error"]["message"]

    resized = MobileSession(tmp_path / "b", package=PACKAGE, clock=clock)
    _feed(resized, clock, frame, 1)
    result = _feed(resized, clock, cv2.resize(frame, (frame.shape[1] // 2, frame.shape[0] // 2)), 1)[0]
    assert result["stopped"] and "Screen size changed" in result["error"]["message"]


def test_unknown_timeout_stops_session(tmp_path: Path) -> None:
    clock = FakeClock()
    session = MobileSession(tmp_path, package=PACKAGE, clock=clock)
    blank = np.zeros((640, 296, 3), dtype=np.uint8)
    result = None
    for _ in range(400):
        clock.advance(0.5)
        result = session.process_frame(blank, foreground_package=PACKAGE)
        if result["stopped"]:
            break
    assert result is not None and result["stopped"]
    assert result["stop_reason"].startswith("state_timeout:unknown")
    assert result["action"] is None


def test_live_requires_package_and_dispatcher(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        MobileSession(tmp_path, send_actions=True, package=PACKAGE)
    with pytest.raises(ValueError):
        MobileSession(tmp_path, send_actions=True, dispatcher=RecordingDispatcher())
    with pytest.raises(ValueError):
        MobileSession(tmp_path, max_rounds=1000)


def test_create_session_and_rgba_json_entry(tmp_path: Path) -> None:
    session = create_session(str(tmp_path), 3, False, PACKAGE, None, None)
    frame = _fixture("qte.jpg")
    data, stride = _rgba_bytes(frame, padding=16)
    payload = json.loads(session.process_rgba(data, frame.shape[1], frame.shape[0], stride, None, False, PACKAGE))
    assert payload["max_rounds"] == 3
    assert payload["stopped"] is False
    assert payload["analysis_ms"] is not None
    assert payload["next_interval_s"] > 0


def test_stop_requested_blocks_dispatch(tmp_path: Path) -> None:
    class StoppingDispatcher(RecordingDispatcher):
        def stopRequested(self) -> bool:  # noqa: N802
            return True

    clock = FakeClock()
    dispatcher = StoppingDispatcher()
    session = MobileSession(tmp_path, package=PACKAGE, send_actions=True, dispatcher=dispatcher, clock=clock)
    frame = _fixture("waiting.jpg")
    session.set_device_size(frame.shape[1], frame.shape[0])
    results = _feed(session, clock, frame, 6)
    assert dispatcher.calls == []
    assert results[-1]["stopped"] and results[-1]["stop_reason"] == "user_stop"
    assert json.loads(session.finish())["stop_reason"] == "user_stop"


def test_self_test_reports_fixture_hints() -> None:
    from fishing_mvp.mobile import self_test

    report = json.loads(self_test(str(FIXTURES), repeats=1))
    assert report["ok"] is True
    assert [item["name"] for item in report["frames"]] == ["waiting", "prompt", "qte", "quality", "result"]
    assert report["opencv"]


def test_auto_retry_switch_overrides_user_config(tmp_path: Path) -> None:
    # The app's switch is what the user sees, so it must win over a config file
    # that says the opposite; otherwise the screen and the run would disagree.
    user_yaml = tmp_path / "user.yaml"
    user_yaml.write_text("automation:\n  auto_retry_enabled: false\n", encoding="utf-8")
    sessions = {
        "file_only": MobileSession(tmp_path / "a", config_path=user_yaml),
        "switch_on": MobileSession(tmp_path / "b", config_path=user_yaml, auto_retry=True),
        "switch_off": MobileSession(tmp_path / "c", auto_retry=False),
        "via_factory": create_session(str(tmp_path / "d"), 1, False, None, str(user_yaml), None, True),
    }
    try:
        enabled = {name: s.config.automation.auto_retry_enabled for name, s in sessions.items()}
    finally:
        for session in sessions.values():
            session.stop()
    assert enabled == {"file_only": False, "switch_on": True, "switch_off": False, "via_factory": True}


def _count_full_button_searches(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    from fishing_mvp import vision

    calls: list[int] = []
    original = vision.detect_action_button

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(vision, "detect_action_button", counting)
    return calls


def test_button_is_tracked_locally_instead_of_searched_every_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The full Hough search is ~3x slower than checking around the remembered button, and it used
    # to run on every frame outside QTE, including the first QTE frame when speed matters most.
    clock = FakeClock()
    calls = _count_full_button_searches(monkeypatch)
    session = MobileSession(tmp_path, package=PACKAGE, clock=clock)
    _feed(session, clock, _fixture("waiting.jpg"), 8)
    session.stop()
    assert len(calls) <= 2, f"full search ran {len(calls)} times for 8 identical frames"


def test_result_screen_still_gets_a_full_search_and_is_recognised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Local tracking is only safe because it falls back when the button is not where it was:
    # the result screen has no control at the old spot and must still be classified.
    clock = FakeClock()
    calls = _count_full_button_searches(monkeypatch)
    session = MobileSession(tmp_path, package=PACKAGE, clock=clock)
    _feed(session, clock, _fixture("waiting.jpg"), 6)
    before = len(calls)
    results = _feed(session, clock, _fixture("result.jpg"), 8)
    session.stop()
    assert results[-1]["state"] == "result"
    assert len(calls) > before, "moving to the result screen never fell back to the full search"


def test_phone_radius_defaults_yield_to_the_users_config(tmp_path: Path) -> None:
    user_yaml = tmp_path / "user.yaml"
    user_yaml.write_text("detector:\n  button_max_radius_ratio: 0.21\n", encoding="utf-8")
    sessions = [MobileSession(tmp_path / "a"), MobileSession(tmp_path / "b", config_path=user_yaml)]
    try:
        plain, custom = (s.config.detector for s in sessions)
    finally:
        for session in sessions:
            session.stop()
    assert (plain.button_min_radius_ratio, plain.button_max_radius_ratio) == (0.06, 0.19)
    # The user's own value is kept; only the field they did not set gets the phone default.
    assert (custom.button_min_radius_ratio, custom.button_max_radius_ratio) == (0.06, 0.21)
