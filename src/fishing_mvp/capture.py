"""Video and ADB frame sources used by the offline and live runners."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterator

import cv2
import numpy as np

from .actions import ADBController


@dataclass(frozen=True)
class FramePacket:
    frame: np.ndarray
    frame_index: int
    timestamp_s: float


class VideoFrameSource:
    def __init__(self, path: str, max_seconds: float | None = None):
        self.path = path
        self.max_seconds = max_seconds

    def __iter__(self) -> Iterator[FramePacket]:
        capture = cv2.VideoCapture(self.path)
        if not capture.isOpened():
            raise RuntimeError(f"Unable to open video: {self.path}")
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0)
        fps = fps if fps > 0 else 30.0
        index = 0
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                timestamp = float(capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0)
                if timestamp <= 0:
                    timestamp = index / fps
                if self.max_seconds is not None and timestamp > self.max_seconds:
                    break
                yield FramePacket(frame=frame, frame_index=index, timestamp_s=timestamp)
                index += 1
        finally:
            capture.release()


class ADBFrameSource:
    def __init__(self, controller: ADBController, fps: float = 10.0):
        self.controller = controller
        self.fps = max(0.5, fps)

    def read(self) -> np.ndarray:
        png = self.controller.screenshot_png()
        image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("ADB returned an unreadable PNG screenshot")
        return image


def scrcpy_status() -> dict[str, object]:
    """Report optional scrcpy availability without requiring it at import time."""

    executable = shutil.which("scrcpy")
    version = None
    if executable:
        try:
            result = subprocess.run([executable, "--version"], capture_output=True, timeout=3, check=False)
            version = (result.stdout or result.stderr).decode("utf-8", errors="replace").splitlines()[0:2]
        except (OSError, subprocess.TimeoutExpired):
            version = None
    return {"available": executable is not None, "path": executable, "version": version}
