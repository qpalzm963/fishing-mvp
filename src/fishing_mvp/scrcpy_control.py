"""Minimal scrcpy 4.1 control-socket client for touch taps.

The client accepts an already-connected socket for the scrcpy control channel.
It deliberately does not create an ADB tunnel, start the scrcpy server, or
reuse the video socket; those lifecycle concerns belong to the integration
layer.  The wire format is the internal scrcpy 4.1
``INJECT_TOUCH_EVENT`` message format and must be kept in sync with the
matching server version.

Both tap methods require the device framebuffer size because scrcpy includes
the screen width and height in every touch message.  ``tap_normalized`` uses
the same top-left-origin normalized coordinate convention as the detector and
maps it to the device framebuffer, not to a downsampled processing frame.
"""

from __future__ import annotations

import math
import operator
import socket
import struct
import threading
from numbers import Real
from typing import Protocol, TypeAlias


SCRCPY_CONTROL_PROTOCOL_VERSION = "4.1"

# Values from scrcpy's Android input/control protocol.
INJECT_TOUCH_EVENT = 2
ACTION_DOWN = 0
ACTION_UP = 1
POINTER_ID_GENERIC_FINGER = (1 << 64) - 2  # UINT64_C(-2)
PRESSURE_DOWN = 0xFFFF
PRESSURE_UP = 0
NO_BUTTONS = 0

# type, action, pointer_id, x, y, framebuffer width/height, pressure,
# action button, buttons.  All fields after the type are big-endian.
_TOUCH_EVENT = struct.Struct(">BBQiiHHHII")
TOUCH_EVENT_SIZE = _TOUCH_EVENT.size
FramebufferSize: TypeAlias = tuple[int, int]


class ScrcpyControlError(RuntimeError):
    """Raised when a control message cannot be validated or sent."""


class _SocketLike(Protocol):
    def send(self, data: bytes | bytearray | memoryview) -> int:
        ...

    def close(self) -> None:
        ...


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ScrcpyControlError(f"{name} must be an integer, not bool")
    try:
        return operator.index(value)
    except TypeError as exc:
        raise ScrcpyControlError(f"{name} must be an integer") from exc


def _validate_framebuffer_size(framebuffer_size: FramebufferSize) -> FramebufferSize:
    try:
        width_value, height_value = framebuffer_size
    except (TypeError, ValueError) as exc:
        raise ScrcpyControlError("framebuffer_size must contain width and height") from exc
    width = _integer(width_value, "framebuffer width")
    height = _integer(height_value, "framebuffer height")
    # scrcpy 4.1 serializes both dimensions as unsigned 16-bit values.
    if not 1 <= width <= 0xFFFF or not 1 <= height <= 0xFFFF:
        raise ScrcpyControlError(
            "framebuffer dimensions must be in the scrcpy u16 range: "
            f"{width}x{height}"
        )
    return width, height


def _validate_pixel_point(
    x: object,
    y: object,
    framebuffer_size: FramebufferSize,
) -> tuple[int, int, FramebufferSize]:
    width, height = _validate_framebuffer_size(framebuffer_size)
    pixel_x = _integer(x, "x")
    pixel_y = _integer(y, "y")
    if not 0 <= pixel_x < width or not 0 <= pixel_y < height:
        raise ScrcpyControlError(
            f"pixel coordinate ({pixel_x}, {pixel_y}) is outside framebuffer {width}x{height}"
        )
    return pixel_x, pixel_y, (width, height)


def _normalized(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ScrcpyControlError(f"{name} must be a real number in [0.0, 1.0]")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ScrcpyControlError(f"{name} must be a finite number in [0.0, 1.0]")
    return normalized


def _normalized_to_pixels(
    x_norm: object,
    y_norm: object,
    framebuffer_size: FramebufferSize,
) -> tuple[int, int, FramebufferSize]:
    width, height = _validate_framebuffer_size(framebuffer_size)
    normalized_x = _normalized(x_norm, "x_norm")
    normalized_y = _normalized(y_norm, "y_norm")
    # Match the existing live coordinate mapping: normalized 1.0 is clamped
    # to the last valid pixel rather than producing width/height.
    pixel_x = min(width - 1, max(0, round(normalized_x * width)))
    pixel_y = min(height - 1, max(0, round(normalized_y * height)))
    return pixel_x, pixel_y, (width, height)


def _encode_touch_event(
    *,
    action: int,
    x: int,
    y: int,
    framebuffer_size: FramebufferSize,
    pressure: int,
) -> bytes:
    width, height = framebuffer_size
    return _TOUCH_EVENT.pack(
        INJECT_TOUCH_EVENT,
        action,
        POINTER_ID_GENERIC_FINGER,
        x,
        y,
        width,
        height,
        pressure,
        NO_BUTTONS,
        NO_BUTTONS,
    )


class ScrcpyControlClient:
    """Send serialized scrcpy 4.1 touch events over a connected socket.

    Parameters
    ----------
    sock:
        A connected socket for the scrcpy control channel.  The client owns
        it and closes it from :meth:`close` or when a send fails.

    Notes
    -----
    The scrcpy protocol is internal and versioned with the server.  This
    class intentionally targets 4.1 and performs no version negotiation.
    """

    def __init__(self, sock: _SocketLike) -> None:
        self._socket: _SocketLike | None = sock
        self._closed = False
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        """Whether the client can no longer send control messages."""

        with self._lock:
            return self._closed

    def __enter__(self) -> "ScrcpyControlClient":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the owned socket; repeated calls are safe."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            sock = self._socket
            self._socket = None
            self._close_socket(sock)

    def tap_pixels(
        self,
        x: int,
        y: int,
        *,
        framebuffer_size: FramebufferSize,
    ) -> None:
        """Inject a single-finger tap at a device pixel coordinate.

        ``framebuffer_size`` is required because it is part of the scrcpy
        touch-event wire message.  Coordinates use a top-left origin and
        must be inside ``[0, width) x [0, height)``.
        """

        pixel_x, pixel_y, size = _validate_pixel_point(x, y, framebuffer_size)
        self._tap(pixel_x, pixel_y, size)

    def tap_normalized(
        self,
        x_norm: float,
        y_norm: float,
        *,
        framebuffer_size: FramebufferSize,
    ) -> None:
        """Inject a tap from top-left-origin normalized coordinates.

        Both normalized values must be finite and within ``[0.0, 1.0]``.
        The device framebuffer size is explicit and is used for both wire
        metadata and coordinate conversion.
        """

        pixel_x, pixel_y, size = _normalized_to_pixels(x_norm, y_norm, framebuffer_size)
        self._tap(pixel_x, pixel_y, size)

    def _tap(self, x: int, y: int, framebuffer_size: FramebufferSize) -> None:
        down = _encode_touch_event(
            action=ACTION_DOWN,
            x=x,
            y=y,
            framebuffer_size=framebuffer_size,
            pressure=PRESSURE_DOWN,
        )
        up = _encode_touch_event(
            action=ACTION_UP,
            x=x,
            y=y,
            framebuffer_size=framebuffer_size,
            pressure=PRESSURE_UP,
        )
        with self._lock:
            self._ensure_open()
            try:
                self._send_all(down)
                self._send_all(up)
            except ScrcpyControlError:
                raise
            except Exception as exc:
                self._fail_locked("failed to send scrcpy touch event", exc)

    def _send_all(self, payload: bytes) -> None:
        sock = self._socket
        if sock is None:
            raise ScrcpyControlError("scrcpy control socket is closed")
        view = memoryview(payload)
        sent = 0
        while sent < len(view):
            try:
                written = sock.send(view[sent:])
            except Exception as exc:
                self._fail_locked("failed to send scrcpy control message", exc)
            if not isinstance(written, int) or written <= 0:
                self._fail_locked(
                    "scrcpy control socket send returned no bytes"
                )
            remaining = len(view) - sent
            if written > remaining:
                self._fail_locked(
                    f"scrcpy control socket send returned too many bytes: {written} > {remaining}"
                )
            sent += written

    def _ensure_open(self) -> None:
        if self._closed or self._socket is None:
            raise ScrcpyControlError("scrcpy control socket is closed")

    def _fail_locked(self, message: str, cause: BaseException | None = None) -> None:
        sock = self._socket
        self._closed = True
        self._socket = None
        self._close_socket(sock)
        error = ScrcpyControlError(message)
        if cause is None:
            raise error
        raise error from cause

    @staticmethod
    def _close_socket(sock: _SocketLike | None) -> None:
        if sock is None:
            return
        shutdown = getattr(sock, "shutdown", None)
        if shutdown is not None:
            try:
                shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            sock.close()
        except OSError:
            pass
