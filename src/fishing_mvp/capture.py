"""Video, ADB, and optional scrcpy frame sources."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Iterator, Protocol

import cv2
import numpy as np

from .actions import ADBController, ADBError
from .models import FrameMetadata
from .scrcpy_stream import ScrcpyError, ScrcpyFrameSource, scrcpy_status


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
        self.frame_count = 0
        self.started_at = time.monotonic()
        self.last_frame_metadata: FrameMetadata | None = None

    def read(self) -> np.ndarray:
        capture_started = time.monotonic()
        png = self.controller.screenshot_png()
        image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("ADB returned an unreadable PNG screenshot")
        decoded_at = time.monotonic()
        self.frame_count += 1
        self.last_frame_metadata = FrameMetadata(
            source_mode="adb",
            frame_index=self.frame_count - 1,
            packet_received_at_monotonic=capture_started,
            decoded_at_monotonic=decoded_at,
            read_at_monotonic=time.monotonic(),
        )
        return image

    def close(self) -> None:
        return None

    def info(self) -> dict[str, object]:
        elapsed = max(0.0, time.monotonic() - self.started_at)
        return {
            "mode": "adb",
            "frame_size": None,
            "frames_captured": self.frame_count,
            "captured_fps": round(self.frame_count / elapsed, 2) if elapsed > 0 else 0.0,
        }


class LiveFrameSource(Protocol):
    def read(self) -> np.ndarray:
        ...

    def close(self) -> None:
        ...

    def info(self) -> dict[str, object]:
        ...


def create_live_frame_source(
    controller: ADBController,
    *,
    mode: str,
    fps: float,
    scrcpy_max_size: int = 0,
    scrcpy_max_fps: int = 30,
    scrcpy_video_bit_rate: int = 8_000_000,
    scrcpy_connect_timeout_s: float = 10.0,
    scrcpy_frame_timeout_s: float = 3.0,
) -> tuple[LiveFrameSource, str | None]:
    """Start a live source, optionally falling back to ADB screenshots."""

    if mode not in {"auto", "scrcpy", "adb"}:
        raise ValueError(f"unsupported capture mode: {mode}")
    if mode == "adb":
        return ADBFrameSource(controller, fps), None

    try:
        source = ScrcpyFrameSource(
            controller,
            max_size=scrcpy_max_size,
            max_fps=scrcpy_max_fps,
            video_bit_rate=scrcpy_video_bit_rate,
            connect_timeout_s=scrcpy_connect_timeout_s,
            frame_timeout_s=scrcpy_frame_timeout_s,
        )
        source.start()
        return source, None
    except (ScrcpyError, ADBError) as exc:
        if mode == "scrcpy":
            raise
        return ADBFrameSource(controller, fps), f"scrcpy unavailable; fell back to ADB screenshot: {exc}"
