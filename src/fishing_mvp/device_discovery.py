"""Safe ADB device discovery for the future portable launcher.

The discovery boundary is deliberately independent from the CLI and from
``ADBController``.  Callers inject a runner, which makes parsing and device
selection deterministic in tests and lets the Windows launcher pass its
bundled ``adb.exe`` explicitly.

Future CLI contract
-------------------
The planned command is::

    FishingMVP.exe discover-device --adb-path <bundled adb> --json

On success it must exit with code 0 and write one JSON object to stdout::

    {"ok": true, "serial": "<serial>", "state": "device", "devices": [...]}

On a discovery or selection error it must exit with code 2 and write one
JSON object to stdout.  The object has ``ok: false`` and an error code from
the table below; it must never return a serial in an error response::

    {"ok": false, "error": {"code": "no_devices", "message": "...", "devices": [...]}}

The selection codes are ``no_devices``, ``multiple_authorized_devices``,
``unauthorized``, ``offline``, ``mixed_devices``, and
``unsupported_state``.  A failed ADB invocation uses ``adb_unavailable`` or
``adb_command_failed``.  Exactly one row with state ``device`` is required;
one authorized device mixed with any other row is still an error.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
import subprocess
from pathlib import Path
from typing import TypeAlias


AUTHORIZED_STATE = "device"
UNAUTHORIZED_STATES = frozenset({"unauthorized", "no permissions"})


@dataclass(frozen=True, slots=True)
class AdbDevice:
    """One device row returned by ``adb devices -l``."""

    serial: str
    state: str
    details: tuple[str, ...] = ()
    raw_line: str = ""

    @property
    def authorized(self) -> bool:
        return self.state == AUTHORIZED_STATE

    def to_dict(self) -> dict[str, object]:
        return {
            "serial": self.serial,
            "state": self.state,
            "details": list(self.details),
        }


@dataclass(frozen=True, slots=True)
class RunnerResult:
    """Small subprocess result shape accepted by :class:`DeviceDiscovery`."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner: TypeAlias = Callable[[Sequence[str]], RunnerResult]


class DeviceDiscoveryError(RuntimeError):
    """A categorized, fail-safe device discovery or selection error."""

    def __init__(
        self,
        code: str,
        message: str,
        devices: Iterable[AdbDevice] = (),
        *,
        returncode: int | None = None,
    ) -> None:
        self.code = code
        self.devices = tuple(devices)
        self.returncode = returncode
        super().__init__(message)

    @property
    def category(self) -> str:
        """Alias used by launcher-facing code that calls these categories."""

        return self.code

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "message": str(self),
            "devices": [device.to_dict() for device in self.devices],
        }
        if self.returncode is not None:
            payload["returncode"] = self.returncode
        return payload


def _as_text(output: str | bytes) -> str:
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    if isinstance(output, str):
        return output
    raise TypeError("adb devices output must be str or bytes")


def parse_adb_devices(output: str | bytes) -> tuple[AdbDevice, ...]:
    """Parse device rows from ``adb devices`` or ``adb devices -l`` output.

    Header, daemon, and error lines are ignored.  Unknown device states are
    retained instead of being treated as authorized, so the selector can
    fail safely rather than silently choosing a row.
    """

    devices: list[AdbDevice] = []
    for raw_line in _as_text(output).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split()
        if not fields:
            continue
        first = fields[0].casefold()
        if first.startswith("*") or first in {"error:", "adb:"}:
            continue
        if len(fields) >= 3 and fields[0].casefold() == "list" and fields[1].casefold() == "of":
            continue
        if len(fields) < 2:
            continue

        serial = fields[0]
        if fields[1].casefold() == "no" and len(fields) >= 3 and fields[2].casefold() == "permissions":
            state = "no permissions"
            detail_start = 3
        else:
            state = fields[1].casefold()
            detail_start = 2
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                details=tuple(fields[detail_start:]),
                raw_line=line,
            )
        )
    return tuple(devices)


def _describe_devices(devices: Sequence[AdbDevice]) -> str:
    return ", ".join(f"{device.serial}={device.state}" for device in devices)


def select_authorized_device(devices: Iterable[AdbDevice]) -> AdbDevice:
    """Return the only authorized device, or raise a categorized error.

    A single ``device`` row is the only successful selection.  In
    particular, an authorized row alongside an unauthorized/offline row is
    classified as ``mixed_devices`` and is never auto-selected.
    """

    rows = tuple(devices)
    if not rows:
        raise DeviceDiscoveryError(
            "no_devices",
            "找不到任何 ADB 裝置；請確認 USB 偵錯已開啟且裝置已連線。",
            rows,
        )

    authorized = tuple(device for device in rows if device.authorized)
    description = _describe_devices(rows)
    if len(authorized) > 1:
        raise DeviceDiscoveryError(
            "multiple_authorized_devices",
            f"偵測到多台已授權 ADB 裝置（{description}）；為安全起見不會自動選擇。",
            rows,
        )
    if len(authorized) == 1:
        if len(rows) == 1:
            return authorized[0]
        raise DeviceDiscoveryError(
            "mixed_devices",
            f"偵測到混合裝置狀態（{description}）；請只保留一台已授權且在線裝置。",
            rows,
        )

    states = {device.state for device in rows}
    if states and states <= UNAUTHORIZED_STATES:
        raise DeviceDiscoveryError(
            "unauthorized",
            f"裝置尚未授權 ADB（{description}）；請在手機上允許 USB 偵錯。",
            rows,
        )
    if states == {"offline"}:
        raise DeviceDiscoveryError(
            "offline",
            f"ADB 裝置目前離線（{description}）；請重新連線裝置。",
            rows,
        )
    if len(states) > 1:
        raise DeviceDiscoveryError(
            "mixed_devices",
            f"偵測到混合裝置狀態（{description}）；請排除未授權或離線裝置。",
            rows,
        )
    raise DeviceDiscoveryError(
        "unsupported_state",
        f"沒有可用的已授權裝置狀態（{description}）。",
        rows,
    )


def _subprocess_runner(command: Sequence[str], timeout_s: float) -> RunnerResult:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DeviceDiscoveryError(
            "adb_unavailable",
            f"找不到指定的 ADB 執行檔：{command[0]}",
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeviceDiscoveryError(
            "adb_unavailable",
            f"無法執行 ADB 裝置偵測：{exc}",
        ) from exc
    return RunnerResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


class DeviceDiscovery:
    """Run ``adb devices -l`` through an injectable command runner."""

    def __init__(
        self,
        adb_path: str | Path = "adb",
        *,
        runner: Runner | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.adb_path = str(adb_path)
        self.timeout_s = timeout_s
        self._runner = runner

    def _run(self, command: Sequence[str]) -> RunnerResult:
        if self._runner is not None:
            try:
                result = self._runner(command)
            except DeviceDiscoveryError:
                raise
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DeviceDiscoveryError(
                    "adb_unavailable",
                    f"無法執行 ADB 裝置偵測：{exc}",
                ) from exc
            if not isinstance(result, RunnerResult):
                raise DeviceDiscoveryError(
                    "adb_unavailable",
                    "裝置偵測 runner 回傳了無效結果。",
                )
            return result
        return _subprocess_runner(command, self.timeout_s)

    def discover(self) -> AdbDevice:
        """Return the only authorized device or raise ``DeviceDiscoveryError``."""

        command = (self.adb_path, "devices", "-l")
        result = self._run(command)
        if result.returncode != 0:
            detail = result.stderr.strip() or "未提供錯誤訊息"
            raise DeviceDiscoveryError(
                "adb_command_failed",
                f"ADB 裝置偵測失敗（exit code {result.returncode}）：{detail}",
                returncode=result.returncode,
            )
        return select_authorized_device(parse_adb_devices(result.stdout))

    def payload(self) -> dict[str, object]:
        """Build the JSON-compatible payload reserved for the future CLI."""

        try:
            device = self.discover()
        except DeviceDiscoveryError as exc:
            return {"ok": False, "error": exc.to_dict()}
        return {
            "ok": True,
            "serial": device.serial,
            "state": device.state,
            "devices": [device.to_dict()],
        }
