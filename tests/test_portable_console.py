import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from fishing_mvp import portable_console as ui
from fishing_mvp.device_discovery import DeviceDiscoveryError
from fishing_mvp.models import FishingState
from fishing_mvp.portable_session import PortableSession, SessionBusy, process_matches, read_json, write_json
from fishing_mvp.runtime import PortablePaths


def reader(*answers):
    values = iter(answers)
    return lambda prompt: next(values)


@pytest.fixture
def portable(tmp_path, monkeypatch):
    paths = PortablePaths.from_root(tmp_path / "空 白 & package")
    paths.scrcpy_dir.mkdir(parents=True)
    paths.config_dir.mkdir()
    for path in paths.missing_bundled_files():
        path.touch()
    paths.default_config.write_text("{}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(ui, "DeviceDiscovery", lambda adb: SimpleNamespace(
        discover=lambda: SimpleNamespace(serial="phone-1")))
    monkeypatch.setattr(ui, "ADBController", lambda *a, **k: SimpleNamespace(foreground_package=lambda: "com.game"))

    def fake_live(**kwargs):
        calls.append(kwargs)
        print("raw per-frame diagnostics")
        kwargs["on_progress"](FishingState.QTE, 0, kwargs["max_rounds"], 1.0)
        return {"stop_reason": "completed_rounds", "completed_rounds": kwargs["max_rounds"]}

    monkeypatch.setattr(ui, "run_live", fake_live)
    return paths, calls


def test_round_input_defaults_retries_and_cancels():
    output = io.StringIO()
    assert ui.ask_rounds(reader("0", "1000", "abc", "1.5", ""), ui.Console(output)) == 1
    assert output.getvalue().count("請重新輸入") == 4
    assert ui.ask_rounds(reader("999"), ui.Console(output)) == 999
    assert ui.ask_rounds(reader(" Q "), ui.Console(output)) is None


def test_console_runs_in_same_output_with_human_progress_and_saved_diagnostics(portable):
    paths, calls = portable
    output = io.StringIO()
    assert ui.start_console(paths, read_input=reader(""), output=output) == 0
    text = output.getvalue()
    assert "[完成] 已完成 1 / 1 輪" in text
    assert "釣魚操作中" in text
    assert "raw per-frame" not in text
    assert "raw per-frame" in next((paths.root / "run/sessions").glob("*/diagnostics.log")).read_text()
    assert calls[0]["capture_mode"] == "scrcpy"
    assert calls[0]["send_actions"] and calls[0]["full_auto"]
    assert calls[0]["package"] == "com.game"
    assert calls[0]["adb_path"] == str(paths.adb)
    assert not (paths.root / "run/session.json").exists()


def test_console_passes_retry_user_config_to_same_full_auto_session(portable):
    paths, calls = portable
    paths.user_config.write_text(
        "automation:\n  auto_retry_enabled: true\n  max_retry_attempts: 2\n"
        "  retry_delay_ms: 500\n  retry_recovery_timeout_s: 6\n"
    )
    assert ui.start_console(paths, read_input=reader("3"), output=io.StringIO()) == 0
    assert len(calls) == 1
    assert calls[0]["max_rounds"] == 3
    config = calls[0]["config"].automation
    assert config.auto_retry_enabled
    assert config.max_retry_attempts == 2
    assert config.retry_delay_ms == 500
    assert config.retry_recovery_timeout_s == 6


def test_console_retries_discovery_and_rechecks_device(portable, monkeypatch):
    paths, calls = portable
    devices = iter([None, "phone-2"])
    def discover():
        serial = next(devices)
        if serial is None:
            raise DeviceDiscoveryError("unauthorized", "unauthorized")
        return SimpleNamespace(serial=serial)
    monkeypatch.setattr(ui, "DeviceDiscovery", lambda path: SimpleNamespace(discover=discover))
    output = io.StringIO()
    assert ui.start_console(paths, read_input=reader("2", "r"), output=output) == 0
    assert "允許 USB 偵錯" in output.getvalue()
    assert calls[0]["serial"] == "phone-2"
    assert len(list((paths.root / "run/sessions").iterdir())) == 2


def test_console_retry_after_live_error_preserves_both_runs(portable, monkeypatch):
    paths, calls = portable
    original = ui.run_live
    def fail_once(**kwargs):
        if not calls:
            calls.append(kwargs)
            write_json(kwargs["output_dir"] / "live_summary.json", {"completed_rounds": 1})
            raise RuntimeError("scrcpy stream interrupted")
        return original(**kwargs)
    monkeypatch.setattr(ui, "run_live", fail_once)
    output = io.StringIO()
    assert ui.start_console(paths, read_input=reader("3", "r"), output=output) == 0
    assert "[未完成] 已完成 1 / 3 輪" in output.getvalue()
    assert "新的 3 輪" in output.getvalue()
    assert "[完成] 已完成 3 / 3 輪" in output.getvalue()
    folders = list((paths.root / "run/sessions").iterdir())
    assert len(folders) == 2
    assert any("scrcpy stream interrupted" in (folder / "diagnostics.log").read_text() for folder in folders)


@pytest.mark.parametrize("failure", ["missing", "config", "foreground"])
def test_preflight_failure_never_sends_input(portable, monkeypatch, failure):
    paths, calls = portable
    if failure == "missing":
        paths.adb.unlink()
    elif failure == "config":
        paths.user_config.write_text("capture_fps: nope")
    else:
        monkeypatch.setattr(ui, "ADBController", lambda *a, **kw: SimpleNamespace(foreground_package=lambda: None))
    output = io.StringIO()
    assert ui.start_console(paths, read_input=reader("", "q"), output=output) == 2
    assert not calls
    assert "[未完成]" in output.getvalue()
    assert "診斷紀錄" in output.getvalue()


@pytest.mark.parametrize("reason", ["user_stop", "keyboard_interrupt", "state_timeout:qte"])
def test_console_distinguishes_stopped_and_timed_out_from_completed(portable, monkeypatch, reason):
    paths, _ = portable
    monkeypatch.setattr(ui, "run_live", lambda **kw: {"stop_reason": reason, "completed_rounds": 1})
    output = io.StringIO()
    code = ui.start_console(paths, read_input=reader("3", "q"), output=output)
    assert "[完成]" not in output.getvalue()
    assert code == (2 if reason.startswith("state_timeout") else 0)
    assert "1 / 3" in output.getvalue()


def test_console_cancel_does_not_touch_device(portable):
    paths, calls = portable
    assert ui.start_console(paths, read_input=reader("q"), output=io.StringIO()) == 0
    assert not calls


def test_progress_throttles_repeated_states_but_shows_round_changes():
    output = io.StringIO()
    console = ui.Console(output)
    for elapsed in (0, 0.1, 0.2, 1, 4):
        console.progress(FishingState.QTE, 0, 2, elapsed)
    assert output.getvalue().count("已完成") == 1
    console.progress(FishingState.QTE, 0, 2, 5)
    console.progress(FishingState.QTE, 1, 2, 5.1)
    assert output.getvalue().count("已完成") == 3


def test_session_lock_and_stale_records_are_scoped(tmp_path):
    root = tmp_path / "run"
    with PortableSession(root) as first:
        first.begin(tmp_path)
        with pytest.raises(SessionBusy):
            with PortableSession(root):
                pass
        assert read_json(root / "session.json")["token"] == first.token
        write_json(root / "stop.request", {"token": "wrong"})
        assert not first.stop_requested()
        write_json(root / "stop.request", {"token": first.token})
        assert first.stop_requested()
        first.finish()
        first.begin(tmp_path)
        assert not first.stop_requested()
    assert not (root / "session.json").exists()
    with PortableSession(root):
        pass


def test_process_identity_rejects_wrong_executable_and_invalid_pid(tmp_path):
    assert process_matches(os.getpid(), Path(sys.executable))
    assert not process_matches(os.getpid(), tmp_path / "other.exe")
    assert not process_matches(-1, Path(sys.executable))
    assert not process_matches(True, Path(sys.executable))


def test_stop_sends_scoped_request_and_waits_for_cleanup(portable, monkeypatch):
    paths, _ = portable
    with PortableSession(paths.root / "run") as session:
        session.begin(paths.root)
        monkeypatch.setattr(ui, "process_matches", lambda pid, path: True)
        def finish_on_sleep(seconds):
            assert session.stop_requested()
            session.finish()
        monkeypatch.setattr(ui.time, "sleep", finish_on_sleep)
        output = io.StringIO()
        assert ui.stop_console(paths, output=output) == 0
        assert "[已停止]" in output.getvalue()


def test_stop_does_not_signal_unverified_pid(portable, monkeypatch):
    paths, _ = portable
    with PortableSession(paths.root / "run") as session:
        session.begin(paths.root)
        monkeypatch.setattr(ui, "process_matches", lambda pid, path: False)
        assert ui.stop_console(paths, output=io.StringIO()) == 2
        assert not session.stop_requested()


def test_stop_timeout_does_not_claim_success_or_kill_process(portable, monkeypatch):
    paths, _ = portable
    with PortableSession(paths.root / "run") as session:
        session.begin(paths.root)
        monkeypatch.setattr(ui, "process_matches", lambda pid, path: True)
        times = iter([0, 11])
        monkeypatch.setattr(ui.time, "monotonic", lambda: next(times))
        output = io.StringIO()
        assert ui.stop_console(paths, output=output) == 2
        assert session.stop_requested()
        assert "[已停止]" not in output.getvalue()


def test_stop_when_idle_is_harmless(portable):
    paths, _ = portable
    output = io.StringIO()
    assert ui.stop_console(paths, output=output) == 0
    assert "沒有正在" in output.getvalue()
