"""ADB input execution with explicit live-mode checks."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

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

    def foreground_package(self) -> str | None:
        result = self._run("shell", "dumpsys", "window", "windows", check=False)
        text = result.stdout.decode("utf-8", errors="replace")
        patterns = (
            r"mCurrentFocus=Window\{[^ ]+ [^ ]+ ([^/\s]+)/",
            r"mFocusedApp=.*? ([^/\s]+)/",
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
