from __future__ import annotations

import socket
from types import SimpleNamespace

import numpy as np

from fishing_mvp.actions import ADBController
from fishing_mvp.capture import ADBFrameSource, create_live_frame_source
from fishing_mvp.live import _map_action_to_device
from fishing_mvp.models import Action, ActionType
from fishing_mvp.scrcpy_stream import (
    CODEC_ID,
    H264_CODEC_ID,
    PACKET_FLAG_CONFIG,
    PACKET_HEADER,
    PACKET_FLAG_SESSION,
    SESSION_META,
    ScrcpyError,
    ScrcpyFrameSource,
    _read_codec_meta,
    _read_video_packet,
)


def test_scrcpy_video_packet_reader_extracts_config_flag_and_payload():
    left, right = socket.socketpair()
    try:
        payload = b"sps-pps"
        right.sendall(PACKET_HEADER.pack(PACKET_FLAG_CONFIG, len(payload)) + payload)
        actual_payload, pts, is_config, session_size = _read_video_packet(left)
    finally:
        left.close()
        right.close()
    assert actual_payload == payload
    assert pts is None
    assert is_config
    assert session_size is None


def test_scrcpy_codec_metadata_reader_accepts_h264():
    left, right = socket.socketpair()
    try:
        right.sendall(CODEC_ID.pack(H264_CODEC_ID) + SESSION_META.pack(PACKET_FLAG_SESSION >> 32, 1080, 2340))
        codec, width, height = _read_codec_meta(left)
    finally:
        left.close()
        right.close()
    assert (codec, width, height) == ("h264", 1080, 2340)


def test_scrcpy_video_packet_reader_extracts_session_metadata():
    left, right = socket.socketpair()
    try:
        right.sendall(SESSION_META.pack(PACKET_FLAG_SESSION >> 32, 760, 1647))
        payload, pts, is_config, session_size = _read_video_packet(left)
    finally:
        left.close()
        right.close()
    assert payload == b""
    assert pts is None
    assert not is_config
    assert session_size == (760, 1647)


def test_adb_frame_source_is_explicit_fallback():
    source, note = create_live_frame_source(ADBController("serial"), mode="adb", fps=10)
    assert isinstance(source, ADBFrameSource)
    assert note is None
    source.close()


def test_foreground_package_parser_uses_current_window_output():
    controller = ADBController("serial")
    controller._run = lambda *args, **kwargs: SimpleNamespace(  # type: ignore[method-assign]
        stdout=b"mCurrentFocus=Window{abc u0 com.example.game/com.example.GameActivity}\n"
    )
    assert controller.foreground_package() == "com.example.game"


def test_display_size_prefers_override_size():
    controller = ADBController("serial")
    controller._run = lambda *args, **kwargs: SimpleNamespace(  # type: ignore[method-assign]
        stdout=b"Physical size: 1080x2340\nOverride size: 900x1950\n"
    )
    assert controller.display_size() == (900, 1950)


def test_normalized_action_coordinates_map_to_device_size():
    action = Action(ActionType.TAP, x=380, y=823, x_norm=0.5, y_norm=0.5)
    mapped = _map_action_to_device(action, (760, 1647), (1080, 2340))
    assert (mapped.x, mapped.y) == (540, 1170)
    assert (mapped.x_norm, mapped.y_norm) == (action.x_norm, action.y_norm)


def test_action_mapping_refuses_aspect_ratio_mismatch():
    action = Action(ActionType.TAP, x=100, y=100, x_norm=0.1, y_norm=0.1)
    try:
        _map_action_to_device(action, (760, 1647), (1080, 1920))
    except RuntimeError as exc:
        assert "aspect ratio mismatch" in str(exc)
    else:
        raise AssertionError("aspect-ratio mismatch was accepted")


def test_scrcpy_packet_reader_rejects_oversized_payload():
    left, right = socket.socketpair()
    try:
        right.sendall(PACKET_HEADER.pack(0, 32 * 1024 * 1024 + 1))
        try:
            _read_video_packet(left)
        except ScrcpyError:
            pass
        else:
            raise AssertionError("oversized scrcpy packet was accepted")
    finally:
        left.close()
        right.close()


def test_scrcpy_frame_source_reuses_latest_static_frame():
    source = ScrcpyFrameSource(ADBController("serial"))
    expected = np.full((4, 3, 3), 17, dtype=np.uint8)
    source.latest_frame = expected
    source.frame_counter = 1
    actual = source.read()
    assert np.array_equal(actual, expected)
    assert actual is not expected
