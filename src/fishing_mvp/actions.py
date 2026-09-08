"""ADB input execution with explicit live-mode checks."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import Action, ActionType


class ADBError(RuntimeError):
    """Raised when ADB cannot capture or inject an input."""


@dataclass
class ADBController:
    serial: str
    timeout_s: float = 4.0

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        command = ["adb", "-s", self.serial, *args]
        try:
            result = subprocess.run(command, capture_output=True, timeout=self.timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ADBError(f"ADB command failed: {' '.join(command)}: {exc}") from exc
        if check and result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ADBError(f"ADB command failed ({result.returncode}): {' '.join(command)}; {detail}")
        return result

    def assert_connected(self) -> None:
        result = subprocess.run(["adb", "devices"], capture_output=True, timeout=self.timeout_s, check=False)
        if result.returncode != 0:
            raise ADBError(result.stderr.decode("utf-8", errors="replace").strip() or "adb devices failed")
        serials = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1] == "device":
                serials.append(fields[0])
        if self.serial not in serials:
            raise ADBError(f"ADB serial is not connected: {self.serial}; connected={serials}")

    def screenshot_png(self) -> bytes:
        return self._run("exec-out", "screencap", "-p").stdout

    def display_size(self) -> tuple[int, int] | None:
        result = self._run("shell", "wm", "size", check=False)
        text = result.stdout.decode("utf-8", errors="replace")
        matches = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", text)
        if not matches:
            return None
        width, height = matches[-1]
        return int(width), int(height)

    def push(self, local_path: str | Path, remote_path: str) -> None:
        self._run("push", str(local_path), remote_path)

    def forward(self, local_port: int, socket_name: str) -> None:
        self._run("forward", f"tcp:{local_port}", f"localabstract:{socket_name}")

    def remove_forward(self, local_port: int) -> None:
        self._run("forward", "--remove", f"tcp:{local_port}", check=False)

    def reverse(self, socket_name: str, local_port: int) -> None:
        self._run("reverse", f"localabstract:{socket_name}", f"tcp:{local_port}")

    def remove_reverse(self, socket_name: str) -> None:
        self._run("reverse", "--remove", f"localabstract:{socket_name}", check=False)

    def popen(self, *args: str) -> subprocess.Popen[str]:
        command = ["adb", "-s", self.serial, *args]
        try:
            return subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise ADBError(f"Unable to start ADB process: {' '.join(command)}: {exc}") from exc

    def foreground_package(self) -> str | None:
        for command in (
            ("shell", "dumpsys", "window"),
            ("shell", "dumpsys", "activity", "activities"),
        ):
            result = self._run(*command, check=False)
            text = result.stdout.decode("utf-8", errors="replace")
            patterns = (
                r"mCurrentFocus=Window\{[^ ]+ [^ ]+ ([^/\s]+)/",
                r"mFocusedApp=.*? ([^/\s]+)/",
                r"mResumedActivity: ActivityRecord\{[^ ]+ [^ ]+ ([^/\s]+)/",
            )
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    return match.group(1)
        return None

    def execute(self, action: Action) -> None:
        if action.action_type == ActionType.NONE:
            return
        if action.x is None or action.y is None:
            raise ADBError("Action has no pixel coordinate")
        if action.action_type == ActionType.TAP:
            if action.hold_ms > 0:
                self._run(
                    "shell",
                    "input",
                    "swipe",
                    str(action.x),
                    str(action.y),
                    str(action.x),
                    str(action.y),
                    str(action.hold_ms),
                )
            else:
                self._run("shell", "input", "tap", str(action.x), str(action.y))
            return
        raise ADBError(f"Unsupported action type for MVP: {action.action_type.value}")
