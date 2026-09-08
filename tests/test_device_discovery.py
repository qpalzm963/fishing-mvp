from __future__ import annotations

import pytest

from fishing_mvp.device_discovery import (
    AdbDevice,
    DeviceDiscovery,
    DeviceDiscoveryError,
    RunnerResult,
    parse_adb_devices,
    select_authorized_device,
)


def test_parse_adb_devices_skips_header_daemon_and_error_lines():
    output = """\
* daemon started successfully *
List of devices attached
ZX1A23B456    device product:demo model:Demo transport_id:1
ZX1A23B457    unauthorized usb:1-2
ZX1A23B458    no permissions usb:1-3
error: a diagnostic line, not a device row
"""

    devices = parse_adb_devices(output)

    assert [(device.serial, device.state) for device in devices] == [
        ("ZX1A23B456", "device"),
        ("ZX1A23B457", "unauthorized"),
        ("ZX1A23B458", "no permissions"),
    ]
    assert devices[0].details == ("product:demo", "model:Demo", "transport_id:1")


def test_parse_adb_devices_accepts_bytes_and_retains_unknown_state():
    devices = parse_adb_devices(b"List of devices attached\nZX-1 charging\n")

    assert devices == (AdbDevice(serial="ZX-1", state="charging", raw_line="ZX-1 charging"),)


def test_select_authorized_device_returns_the_only_online_device():
    device = select_authorized_device([AdbDevice("phone-1", "device")])

    assert device.serial == "phone-1"
    assert device.authorized


@pytest.mark.parametrize(
    ("devices", "code", "message_fragment"),
    [
        ([], "no_devices", "找不到任何 ADB 裝置"),
        (
            [AdbDevice("phone-1", "device"), AdbDevice("phone-2", "device")],
            "multiple_authorized_devices",
            "多台已授權",
        ),
        ([AdbDevice("phone-1", "unauthorized")], "unauthorized", "尚未授權"),
        ([AdbDevice("phone-1", "offline")], "offline", "離線"),
        (
            [AdbDevice("phone-1", "unauthorized"), AdbDevice("phone-2", "offline")],
            "mixed_devices",
            "混合裝置狀態",
        ),
        (
            [AdbDevice("phone-1", "device"), AdbDevice("phone-2", "unauthorized")],
            "mixed_devices",
            "混合裝置狀態",
        ),
    ],
)
def test_select_authorized_device_never_silently_chooses(
    devices: list[AdbDevice],
    code: str,
    message_fragment: str,
):
    with pytest.raises(DeviceDiscoveryError) as raised:
        select_authorized_device(devices)

    error = raised.value
    assert error.code == code
    assert error.category == code
    assert message_fragment in str(error)
    assert [device.serial for device in error.devices] == [device.serial for device in devices]


def test_select_authorized_device_reports_unsupported_single_state():
    with pytest.raises(DeviceDiscoveryError) as raised:
        select_authorized_device([AdbDevice("phone-1", "bootloader")])

    assert raised.value.code == "unsupported_state"
    assert "bootloader" in str(raised.value)


def test_discovery_injects_runner_and_passes_explicit_adb_path():
    calls: list[tuple[str, ...]] = []

    def runner(command):
        calls.append(tuple(command))
        return RunnerResult(
            returncode=0,
            stdout="List of devices attached\nphone-1\tdevice\n",
        )

    discovery = DeviceDiscovery(r"C:\portable\platform-tools\adb.exe", runner=runner)

    selected = discovery.discover()

    assert selected.serial == "phone-1"
    assert calls == [(r"C:\portable\platform-tools\adb.exe", "devices", "-l")]


def test_discovery_payload_has_single_device_on_success():
    discovery = DeviceDiscovery(
        runner=lambda command: RunnerResult(
            returncode=0,
            stdout="List of devices attached\nphone-1\tdevice\n",
        )
    )

    assert discovery.payload() == {
        "ok": True,
        "serial": "phone-1",
        "state": "device",
        "devices": [
            {"serial": "phone-1", "state": "device", "details": []},
        ],
    }


def test_discovery_payload_contains_categorized_selection_error():
    discovery = DeviceDiscovery(
        runner=lambda command: RunnerResult(
            returncode=0,
            stdout=(
                "List of devices attached\n"
                "phone-1\tdevice\n"
                "phone-2\toffline\n"
            ),
        )
    )

    assert discovery.payload() == {
        "ok": False,
        "error": {
            "code": "mixed_devices",
            "message": "偵測到混合裝置狀態（phone-1=device, phone-2=offline）；請只保留一台已授權且在線裝置。",
            "devices": [
                {"serial": "phone-1", "state": "device", "details": []},
                {"serial": "phone-2", "state": "offline", "details": []},
            ],
        },
    }


def test_discovery_reports_adb_command_failure_without_selecting_stdout_rows():
    discovery = DeviceDiscovery(
        runner=lambda command: RunnerResult(
            returncode=1,
            stdout="List of devices attached\nphone-1\tdevice\n",
            stderr="daemon unavailable",
        )
    )

    with pytest.raises(DeviceDiscoveryError) as raised:
        discovery.discover()

    assert raised.value.code == "adb_command_failed"
    assert raised.value.returncode == 1
    assert "daemon unavailable" in str(raised.value)


def test_discovery_wraps_runner_oserror_as_adb_unavailable():
    def runner(command):
        raise OSError("permission denied")

    discovery = DeviceDiscovery(runner=runner)

    with pytest.raises(DeviceDiscoveryError) as raised:
        discovery.discover()

    assert raised.value.code == "adb_unavailable"
    assert raised.value.__cause__ is not None
    assert "permission denied" in str(raised.value)
