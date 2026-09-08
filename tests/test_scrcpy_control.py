from __future__ import annotations

import struct

import pytest

from fishing_mvp.scrcpy_control import (
    ACTION_DOWN,
    ACTION_UP,
    INJECT_TOUCH_EVENT,
    POINTER_ID_GENERIC_FINGER,
    PRESSURE_DOWN,
    PRESSURE_UP,
    ScrcpyControlClient,
    ScrcpyControlError,
    TOUCH_EVENT_SIZE,
)


class FakeSocket:
    def __init__(self, *, max_send: int | None = None, send_error: BaseException | None = None) -> None:
        self.max_send = max_send
        self.send_error = send_error
        self.sent = bytearray()
        self.send_calls = 0
        self.shutdown_calls: list[int] = []
        self.close_calls = 0

    def send(self, data: bytes | bytearray | memoryview) -> int:
        self.send_calls += 1
        if self.send_error is not None:
            raise self.send_error
        chunk = bytes(data)
        if self.max_send is not None:
            chunk = chunk[: self.max_send]
        self.sent.extend(chunk)
        return len(chunk)

    def shutdown(self, how: int) -> None:
        self.shutdown_calls.append(how)

    def close(self) -> None:
        self.close_calls += 1


def unpack_events(payload: bytes) -> list[tuple[object, ...]]:
    assert len(payload) % TOUCH_EVENT_SIZE == 0
    event_struct = struct.Struct(">BBQiiHHHII")
    return [event_struct.unpack_from(payload, offset) for offset in range(0, len(payload), TOUCH_EVENT_SIZE)]


def test_tap_pixels_serializes_scrcpy_41_down_and_up_messages():
    sock = FakeSocket()
    client = ScrcpyControlClient(sock)

    client.tap_pixels(123, 456, framebuffer_size=(1080, 2340))

    assert unpack_events(bytes(sock.sent)) == [
        (
            INJECT_TOUCH_EVENT,
            ACTION_DOWN,
            POINTER_ID_GENERIC_FINGER,
            123,
            456,
            1080,
            2340,
            PRESSURE_DOWN,
            0,
            0,
        ),
        (
            INJECT_TOUCH_EVENT,
            ACTION_UP,
            POINTER_ID_GENERIC_FINGER,
            123,
            456,
            1080,
            2340,
            PRESSURE_UP,
            0,
            0,
        ),
    ]
    assert len(sock.sent) == 2 * TOUCH_EVENT_SIZE


def test_tap_normalized_maps_to_device_framebuffer_and_clamps_one():
    sock = FakeSocket()
    client = ScrcpyControlClient(sock)

    client.tap_normalized(0.5, 1.0, framebuffer_size=(1080, 2340))

    events = unpack_events(bytes(sock.sent))
    assert all(event[3:7] == (540, 2339, 1080, 2340) for event in events)


def test_short_writes_are_completed_for_both_touch_messages():
    sock = FakeSocket(max_send=3)
    client = ScrcpyControlClient(sock)

    client.tap_pixels(1, 2, framebuffer_size=(10, 20))

    assert len(sock.sent) == 2 * TOUCH_EVENT_SIZE
    assert sock.send_calls > 2
    assert not client.closed


@pytest.mark.parametrize(
    ("x", "y", "size"),
    [
        (-1, 0, (10, 20)),
        (10, 0, (10, 20)),
        (0, 20, (10, 20)),
        (0, 0, (0, 20)),
        (0, 0, (10, 70_000)),
    ],
)
def test_tap_pixels_rejects_invalid_coordinates_or_framebuffer(x: int, y: int, size: tuple[int, int]):
    sock = FakeSocket()
    client = ScrcpyControlClient(sock)

    with pytest.raises(ScrcpyControlError):
        client.tap_pixels(x, y, framebuffer_size=size)

    assert sock.sent == b""
    assert not client.closed


@pytest.mark.parametrize(
    ("x_norm", "y_norm"),
    [(-0.01, 0.5), (0.5, 1.01), (float("nan"), 0.5), (float("inf"), 0.5)],
)
def test_tap_normalized_rejects_invalid_values(x_norm: float, y_norm: float):
    sock = FakeSocket()
    client = ScrcpyControlClient(sock)

    with pytest.raises(ScrcpyControlError):
        client.tap_normalized(x_norm, y_norm, framebuffer_size=(10, 20))

    assert sock.sent == b""
    assert not client.closed


def test_send_zero_closes_client_and_raises_control_error():
    sock = FakeSocket(max_send=0)
    client = ScrcpyControlClient(sock)

    with pytest.raises(ScrcpyControlError, match="no bytes"):
        client.tap_pixels(1, 2, framebuffer_size=(10, 20))

    assert client.closed
    assert sock.close_calls == 1
    assert len(sock.shutdown_calls) == 1
    with pytest.raises(ScrcpyControlError, match="closed"):
        client.tap_pixels(1, 2, framebuffer_size=(10, 20))


def test_socket_error_is_wrapped_with_original_cause_and_closes_client():
    cause = OSError("broken pipe")
    sock = FakeSocket(send_error=cause)
    client = ScrcpyControlClient(sock)

    with pytest.raises(ScrcpyControlError, match="failed to send") as raised:
        client.tap_pixels(1, 2, framebuffer_size=(10, 20))

    assert raised.value.__cause__ is cause
    assert client.closed
    assert sock.close_calls == 1


def test_close_is_idempotent_and_still_closes_when_shutdown_fails():
    sock = FakeSocket()
    original_shutdown = sock.shutdown

    def failing_shutdown(how: int) -> None:
        original_shutdown(how)
        raise OSError("already shut down")

    sock.shutdown = failing_shutdown  # type: ignore[method-assign]
    client = ScrcpyControlClient(sock)

    client.close()
    client.close()

    assert client.closed
    assert sock.shutdown_calls == [2]
    assert sock.close_calls == 1


def test_context_manager_closes_owned_socket():
    sock = FakeSocket()

    with ScrcpyControlClient(sock) as client:
        assert not client.closed

    assert client.closed
    assert sock.close_calls == 1
