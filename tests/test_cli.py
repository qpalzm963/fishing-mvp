from __future__ import annotations

import json

import pytest

from fishing_mvp import cli
from fishing_mvp import device_discovery


def test_discover_device_cli_emits_machine_readable_success(monkeypatch, capsys):
    calls: list[str] = []

    class FakeDiscovery:
        def __init__(self, adb_path):
            calls.append(adb_path)

        def payload(self):
            return {
                "ok": True,
                "serial": "phone-1",
                "state": "device",
                "devices": [{"serial": "phone-1", "state": "device", "details": []}],
            }

    monkeypatch.setattr(device_discovery, "DeviceDiscovery", FakeDiscovery)

    assert cli.main(["discover-device", "--adb-path", r"C:\bundle\adb.exe", "--json"]) == 0

    assert calls == [r"C:\bundle\adb.exe"]
    output = capsys.readouterr().out
    assert output.isascii()
    assert json.loads(output) == {
        "ok": True,
        "serial": "phone-1",
        "state": "device",
        "devices": [{"serial": "phone-1", "state": "device", "details": []}],
    }


def test_discover_device_cli_returns_exit_code_two_for_selection_error(monkeypatch, capsys):
    class FakeDiscovery:
        def __init__(self, adb_path):
            pass

        def payload(self):
            return {
                "ok": False,
                "error": {
                    "code": "multiple_authorized_devices",
                    "message": "偵測到多台已授權 ADB 裝置。",
                    "devices": [],
                },
            }

    monkeypatch.setattr(device_discovery, "DeviceDiscovery", FakeDiscovery)

    assert cli.main(["discover-device", "--json"]) == 2

    output = capsys.readouterr().out
    assert output.isascii()
    payload = json.loads(output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "multiple_authorized_devices"


def test_runtime_smoke_cli_emits_success_payload(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_runtime_smoke",
        lambda: {
            "ok": True,
            "config_loaded": True,
            "codec": "h264",
            "decoder_name": "h264",
            "pyav_version": "18.1.0",
        },
    )

    assert cli.main(["runtime-smoke"]) == 0

    assert json.loads(capsys.readouterr().out)["codec"] == "h264"


def test_runtime_smoke_cli_returns_exit_code_two_for_diagnostic_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_runtime_smoke",
        lambda: {
            "ok": False,
            "error": {"code": "pyav_h264_unavailable", "message": "decoder unavailable"},
        },
    )

    assert cli.main(["runtime-smoke"]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "pyav_h264_unavailable"


@pytest.mark.parametrize("value", ["0", "1000", "-1", "abc"])
def test_max_rounds_rejects_values_outside_strict_launcher_contract(value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["live", "--serial", "phone-1", "--max-rounds", value])


@pytest.mark.parametrize("value", ["1", "10", "999"])
def test_max_rounds_accepts_values_in_strict_launcher_contract(value):
    args = cli.build_parser().parse_args(["live", "--serial", "phone-1", "--max-rounds", value])

    assert args.max_rounds == int(value)
