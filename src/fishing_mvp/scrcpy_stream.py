"""Low-latency video frames from the scrcpy server protocol.

The desktop scrcpy binary owns the normal mirror window, so it cannot expose
decoded frames directly to this process.  This module starts the matching
scrcpy server shipped with the installed binary, receives its video socket,
and decodes the H.264 packets with PyAV.  The protocol is intentionally kept
behind this small adapter so the rest of the detector only sees OpenCV BGR
frames.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
from typing import Any, TextIO

import numpy as np

from .actions import ADBController, ADBError
from .models import Action, ActionType, FrameMetadata
from .scrcpy_control import SCRCPY_CONTROL_PROTOCOL_VERSION, ScrcpyControlClient, ScrcpyControlError


REMOTE_SERVER_PATH = "/data/local/tmp/fishing-mvp-scrcpy-server.jar"
DEVICE_SOCKET_PREFIX = "scrcpy"
DEVICE_NAME_FIELD_LENGTH = 64
PACKET_HEADER = struct.Struct(">QI")
CODEC_ID = struct.Struct(">I")
SESSION_META = struct.Struct(">III")
PACKET_FLAG_SESSION = 1 << 63
PACKET_FLAG_CONFIG = 1 << 62
PACKET_FLAG_KEY_FRAME = 1 << 61
PACKET_PTS_MASK = PACKET_FLAG_KEY_FRAME - 1
SESSION_FLAG_U32 = PACKET_FLAG_SESSION >> 32
MAX_VIDEO_PACKET_SIZE = 32 * 1024 * 1024
H264_CODEC_ID = 0x68323634
HEVC_CODEC_ID = 0x68323635
AV1_CODEC_ID = 0x00617631
CODEC_NAMES = {
    H264_CODEC_ID: "h264",
    HEVC_CODEC_ID: "hevc",
    AV1_CODEC_ID: "av1",
}


class ScrcpyError(RuntimeError):
    """Raised when the optional scrcpy video source cannot be started."""


@dataclass(frozen=True)
class ScrcpyInstallation:
    executable: Path
    version: str
    server_path: Path


def _scrcpy_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScrcpyError(f"unable to query scrcpy version: {exc}") from exc
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    match = re.search(r"\bscrcpy\s+([0-9]+(?:\.[0-9]+)+)", output)
    if match is None:
        raise ScrcpyError(f"unable to parse scrcpy version from: {output.strip() or '<empty>'}")
    return match.group(1)


def _server_candidates(executable: Path, version: str) -> list[Path]:
    resolved = executable.resolve()
    roots = [resolved.parent.parent, resolved.parent, Path("/opt/homebrew"), Path("/usr/local")]
    candidates: list[Path] = []
    for root in roots:
        share_dir = root / "share" / "scrcpy"
        candidates.extend(
            [
                share_dir / "scrcpy-server",
                share_dir / f"scrcpy-server-v{version}",
                share_dir / f"scrcpy-server-{version}",
            ]
        )
        if share_dir.is_dir():
            candidates.extend(sorted(share_dir.glob("scrcpy-server*")))
        candidates.append(root / "scrcpy-server")
    candidates.append(resolved.parent / "scrcpy-server")
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            unique.append(candidate)
    return unique


def discover_scrcpy(executable_path: str | Path | None = None) -> ScrcpyInstallation:
    """Find the desktop binary and its matching server jar."""

    if executable_path is None:
        executable_name = shutil.which("scrcpy")
        if executable_name is None:
            raise ScrcpyError("scrcpy is not installed or not available in PATH")
        executable = Path(executable_name)
    else:
        executable = Path(executable_path).expanduser()
        if not executable.is_file():
            raise ScrcpyError(f"scrcpy executable was not found: {executable}")
    version = _scrcpy_version(executable)
    candidates = _server_candidates(executable, version)
    if not candidates:
        raise ScrcpyError(
            f"scrcpy {version} was found at {executable}, but its scrcpy-server file was not found"
        )
    return ScrcpyInstallation(executable=executable, version=version, server_path=candidates[0])


def scrcpy_status(executable_path: str | Path | None = None) -> dict[str, object]:
    """Report scrcpy availability without importing the optional decoder."""

    if executable_path is None:
        executable_name = shutil.which("scrcpy")
        if executable_name is None:
            return {
                "available": False,
                "path": None,
                "version": None,
                "server_path": None,
            }
        executable = Path(executable_name)
    else:
        executable = Path(executable_path).expanduser()
        if not executable.is_file():
            return {
                "available": False,
                "path": str(executable),
                "version": None,
                "server_path": None,
                "error": "scrcpy executable was not found",
            }
    try:
        installation = discover_scrcpy(executable)
    except ScrcpyError as exc:
        return {
            "available": True,
            "path": str(executable),
            "version": None,
            "server_path": None,
            "error": str(exc),
        }
    return {
        "available": True,
        "path": str(installation.executable),
        "version": installation.version,
        "server_path": str(installation.server_path),
    }


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("scrcpy video socket closed while reading")
        data.extend(chunk)
    return bytes(data)


def _read_device_name(sock: socket.socket) -> str:
    raw = _read_exact(sock, DEVICE_NAME_FIELD_LENGTH)
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _read_codec_meta(sock: socket.socket) -> tuple[str, int, int]:
    codec_id = CODEC_ID.unpack(_read_exact(sock, CODEC_ID.size))[0]
    codec_name = CODEC_NAMES.get(codec_id)
    if codec_name is None:
        raise ScrcpyError(f"unsupported scrcpy codec id: 0x{codec_id:08x}")
    session_flags, width, height = SESSION_META.unpack(_read_exact(sock, SESSION_META.size))
    if not session_flags & SESSION_FLAG_U32:
        raise ScrcpyError("invalid scrcpy video session metadata")
    if width <= 0 or height <= 0:
        raise ScrcpyError(f"invalid scrcpy frame size: {width}x{height}")
    return codec_name, width, height


def _read_video_packet(
    sock: socket.socket,
) -> tuple[bytes, int | None, bool, tuple[int, int] | None]:
    header = _read_exact(sock, PACKET_HEADER.size)
    pts_flags, payload_size = PACKET_HEADER.unpack(header)
    if header[0] & 0x80:
        session_flags, width, height = SESSION_META.unpack(header)
        if not session_flags & SESSION_FLAG_U32:
            raise ScrcpyError("invalid scrcpy video session packet")
        if width <= 0 or height <= 0:
            raise ScrcpyError(f"invalid scrcpy frame size: {width}x{height}")
        return b"", None, False, (width, height)
    if payload_size <= 0 or payload_size > MAX_VIDEO_PACKET_SIZE:
        raise ScrcpyError(f"invalid scrcpy packet size: {payload_size}")
    payload = _read_exact(sock, payload_size)
    is_config = bool(pts_flags & PACKET_FLAG_CONFIG)
    pts = None if is_config else pts_flags & PACKET_PTS_MASK
    return payload, pts, is_config, None


class ScrcpyFrameSource:
    """Read the latest decoded frame from an isolated scrcpy server session."""

    def __init__(
        self,
        controller: ADBController,
        *,
        max_size: int = 0,
        max_fps: int = 30,
        video_bit_rate: int = 8_000_000,
        connect_timeout_s: float = 10.0,
        frame_timeout_s: float = 3.0,
        scrcpy_executable: str | Path | None = None,
    ) -> None:
        self.controller = controller
        self.max_size = max(0, int(max_size))
        self.max_fps = max(1, int(max_fps))
        self.video_bit_rate = max(250_000, int(video_bit_rate))
        self.connect_timeout_s = max(1.0, float(connect_timeout_s))
        self.frame_timeout_s = max(0.5, float(frame_timeout_s))
        self.scrcpy_executable = Path(scrcpy_executable).expanduser() if scrcpy_executable is not None else None

        self.installation: ScrcpyInstallation | None = None
        self.server_process: subprocess.Popen[str] | None = None
        self.video_socket: socket.socket | None = None
        self.control_socket: socket.socket | None = None
        self.control_client: ScrcpyControlClient | None = None
        self.listen_socket: socket.socket | None = None
        self.tunnel_mode: str | None = None
        self.last_tunnel_mode: str | None = None
        self.server_logs: deque[str] = deque(maxlen=40)
        self.log_thread: threading.Thread | None = None
        self.decode_thread: threading.Thread | None = None
        self.decoder: Any | None = None
        self.stop_event = threading.Event()
        self.frame_condition = threading.Condition()
        self.reader_error: Exception | None = None
        self.latest_frame: np.ndarray | None = None
        self.latest_frame_metadata: FrameMetadata | None = None
        self.last_frame_metadata: FrameMetadata | None = None
        self._last_read_frame_index: int | None = None
        self.frame_counter = 0
        self.device_name: str | None = None
        self.codec_name: str | None = None
        self.frame_width: int | None = None
        self.frame_height: int | None = None
        self.started_at: float | None = None
        self.socket_name = f"{DEVICE_SOCKET_PREFIX}_{secrets.randbelow(1 << 31):08x}"
        self.local_port = self._pick_free_port()

    def __enter__(self) -> "ScrcpyFrameSource":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def start(self) -> None:
        if self.decode_thread is not None and self.decode_thread.is_alive():
            return
        self.controller.assert_connected()
        self.installation = discover_scrcpy(self.scrcpy_executable)
        if self.installation.version != SCRCPY_CONTROL_PROTOCOL_VERSION:
            raise ScrcpyError(
                "scrcpy control input is version-pinned to "
                f"{SCRCPY_CONTROL_PROTOCOL_VERSION}; found {self.installation.version}"
            )
        self.stop_event.clear()
        self.reader_error = None
        self.latest_frame = None
        self.latest_frame_metadata = None
        self.last_frame_metadata = None
        self._last_read_frame_index = None
        self.control_client = None
        self.control_socket = None
        self.frame_counter = 0
        self.server_logs.clear()

        self.controller.push(self.installation.server_path, REMOTE_SERVER_PATH)
        attempts: list[str] = []
        for mode in ("forward", "reverse"):
            try:
                self._start_with_mode(mode)
                return
            except Exception as exc:
                attempts.append(f"{mode}: {exc}")
                self._cleanup_runtime(remove_tunnel=True)
                self.stop_event.clear()
        details = "\n".join(attempts)
        logs = "\n".join(self.server_logs)
        message = f"unable to start scrcpy stream for {self.installation.version}\n{details}"
        if logs:
            message += f"\nserver logs:\n{logs}"
        raise ScrcpyError(message)

    def close(self) -> None:
        self._cleanup_runtime(remove_tunnel=True)

    def send_action(self, action: Action, device_size: tuple[int, int]) -> str:
        """Send a supported live action through scrcpy's control socket."""

        if action.action_type == ActionType.NONE:
            return "none"
        if action.action_type != ActionType.TAP or action.hold_ms > 0:
            raise ScrcpyError("scrcpy control integration currently supports zero-hold TAP actions only")
        if action.x is None or action.y is None:
            raise ScrcpyError("scrcpy control action has no pixel coordinate")
        client = self.control_client
        if client is None or client.closed:
            raise ScrcpyError("scrcpy control socket is not available")
        try:
            client.tap_pixels(action.x, action.y, framebuffer_size=device_size)
        except ScrcpyControlError as exc:
            raise ScrcpyError(f"scrcpy control input failed: {exc}") from exc
        return "scrcpy_control"

    def read(self) -> np.ndarray:
        deadline = time.monotonic() + self.frame_timeout_s
        with self.frame_condition:
            while (
                self.latest_frame is None
                and not self.reader_error
                and not self.stop_event.is_set()
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ScrcpyError("timed out waiting for a new scrcpy frame")
                self.frame_condition.wait(timeout=remaining)
            if self.reader_error is not None:
                raise ScrcpyError(f"scrcpy video reader failed: {self.reader_error}") from self.reader_error
            if self.latest_frame is None:
                raise ScrcpyError("scrcpy stream ended before a frame was decoded")
            # scrcpy is change-driven: static screens may produce no new
            # packet for several seconds. Reuse the latest frame at the
            # detector's sampling rate instead of treating that as a timeout.
            frame = self.latest_frame.copy()
            metadata = self.latest_frame_metadata
            if metadata is not None:
                read_at = time.monotonic()
                self.last_frame_metadata = FrameMetadata(
                    source_mode=metadata.source_mode,
                    frame_index=metadata.frame_index,
                    frame_pts_us=metadata.frame_pts_us,
                    packet_received_at_monotonic=metadata.packet_received_at_monotonic,
                    decoded_at_monotonic=metadata.decoded_at_monotonic,
                    read_at_monotonic=read_at,
                    reused=metadata.frame_index == self._last_read_frame_index,
                )
                self._last_read_frame_index = metadata.frame_index
            return frame

    def info(self) -> dict[str, object]:
        with self.frame_condition:
            frames_decoded = self.frame_counter
            latest = self.latest_frame
            latest_metadata = self.latest_frame_metadata
        elapsed = max(0.0, time.monotonic() - self.started_at) if self.started_at is not None else 0.0
        width = latest.shape[1] if latest is not None else self.frame_width
        height = latest.shape[0] if latest is not None else self.frame_height
        return {
            "mode": "scrcpy",
            "scrcpy_version": self.installation.version if self.installation else None,
            "device_name": self.device_name,
            "codec": self.codec_name,
            "frame_size": {"width": width, "height": height} if width and height else None,
            "frames_decoded": frames_decoded,
            "decoded_fps": round(frames_decoded / elapsed, 2) if elapsed > 0 else 0.0,
            "tunnel": self.tunnel_mode or self.last_tunnel_mode,
            "latest_frame_pts_us": latest_metadata.frame_pts_us if latest_metadata else None,
            "latest_frame_age_ms": round((time.monotonic() - latest_metadata.decoded_at_monotonic) * 1000.0, 2)
            if latest_metadata and latest_metadata.decoded_at_monotonic is not None
            else None,
        }

    def _start_with_mode(self, mode: str) -> None:
        if self.installation is None:
            raise ScrcpyError("scrcpy installation has not been discovered")
        self.tunnel_mode = mode
        self.last_tunnel_mode = mode
        if mode == "forward":
            self.controller.forward(self.local_port, self.socket_name)
        elif mode == "reverse":
            self.listen_socket = self._open_listener(self.local_port)
            self.controller.reverse(self.socket_name, self.local_port)
        else:
            raise ValueError(f"unsupported tunnel mode: {mode}")

        self._start_server_process(mode)
        if mode == "forward":
            self.video_socket = self._connect_forward_socket()
            if _read_exact(self.video_socket, 1) != b"\x00":
                raise ScrcpyError("invalid scrcpy forward-tunnel handshake")
            self.control_socket = self._connect_forward_socket()
        else:
            self.video_socket = self._accept_reverse_socket()
            self.control_socket = self._accept_reverse_socket()
        self.video_socket.settimeout(None)
        self.control_socket.settimeout(None)
        self.control_client = ScrcpyControlClient(self.control_socket)
        self.device_name = _read_device_name(self.video_socket)
        self.codec_name, self.frame_width, self.frame_height = _read_codec_meta(self.video_socket)
        self.decoder = self._create_decoder(self.codec_name)
        self.started_at = time.monotonic()
        self.decode_thread = threading.Thread(target=self._decode_loop, name="fishing-mvp-scrcpy-decode", daemon=True)
        self.decode_thread.start()

    def _start_server_process(self, mode: str) -> None:
        if self.installation is None:
            raise ScrcpyError("scrcpy installation has not been discovered")
        options = [
            f"scid={self.socket_name.rsplit('_', 1)[-1]}",
            "log_level=warn",
            "video=true",
            "audio=false",
            "control=true",
            "video_codec=h264",
            f"max_size={self.max_size}",
            f"max_fps={self.max_fps}",
            f"video_bit_rate={self.video_bit_rate}",
            "clipboard_autosync=false",
            "cleanup=true",
            "power_on=false",
        ]
        if mode == "forward":
            options.append("tunnel_forward=true")
        self.server_process = self.controller.popen(
            "shell",
            f"CLASSPATH={REMOTE_SERVER_PATH}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            self.installation.version,
            *options,
        )
        self.log_thread = threading.Thread(target=self._capture_server_logs, name="fishing-mvp-scrcpy-log", daemon=True)
        self.log_thread.start()

    def _create_decoder(self, codec_name: str) -> Any:
        if codec_name != "h264":
            raise ScrcpyError(f"scrcpy returned {codec_name}; this MVP currently requires H.264")
        try:
            import av
        except ImportError as exc:
            raise ScrcpyError("scrcpy capture requires PyAV; install with: python -m pip install -e '.[scrcpy]'") from exc
        try:
            return av.CodecContext.create(codec_name, "r")
        except Exception as exc:
            raise ScrcpyError(f"unable to create {codec_name} decoder: {exc}") from exc

    def _decode_loop(self) -> None:
        if self.video_socket is None or self.decoder is None:
            return
        pending_config: bytes | None = None
        try:
            while not self.stop_event.is_set():
                payload, pts, is_config, session_size = _read_video_packet(self.video_socket)
                packet_received_at = time.monotonic()
                if session_size is not None:
                    self.frame_width, self.frame_height = session_size
                    self.decoder = self._create_decoder(self.codec_name or "")
                    pending_config = None
                    continue
                if is_config:
                    pending_config = payload
                    continue
                if pending_config:
                    payload = pending_config + payload
                    pending_config = None
                import av

                packet = av.Packet(payload)
                if pts is not None:
                    packet.pts = pts
                    packet.dts = pts
                for decoded in self.decoder.decode(packet):
                    image = decoded.to_ndarray(format="bgr24")
                    decoded_at = time.monotonic()
                    with self.frame_condition:
                        self.latest_frame = image
                        self.latest_frame_metadata = FrameMetadata(
                            source_mode="scrcpy",
                            frame_index=self.frame_counter,
                            frame_pts_us=pts,
                            packet_received_at_monotonic=packet_received_at,
                            decoded_at_monotonic=decoded_at,
                        )
                        self.frame_counter += 1
                        self.frame_condition.notify_all()
        except (EOFError, OSError) as exc:
            if not self.stop_event.is_set():
                self._set_reader_error(exc)
        except Exception as exc:
            if not self.stop_event.is_set():
                self._set_reader_error(exc)

    def _set_reader_error(self, error: Exception) -> None:
        self.reader_error = error
        with self.frame_condition:
            self.frame_condition.notify_all()

    def _capture_server_logs(self) -> None:
        process = self.server_process
        if process is None or process.stdout is None:
            return
        self._read_process_stream(process.stdout)

    def _read_process_stream(self, stream: TextIO) -> None:
        for line in stream:
            line = line.strip()
            if line:
                self.server_logs.append(line)

    def _connect_forward_socket(self) -> socket.socket:
        deadline = time.monotonic() + self.connect_timeout_s
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection(("127.0.0.1", self.local_port), timeout=1.0)
                sock.settimeout(None)
                return sock
            except OSError as exc:
                last_error = exc
                if self.server_process is not None and self.server_process.poll() is not None:
                    raise ScrcpyError("scrcpy server exited before the forward socket became ready") from exc
                time.sleep(0.05)
        raise ScrcpyError(f"timed out connecting to scrcpy forward socket: {last_error}")

    def _accept_reverse_socket(self) -> socket.socket:
        if self.listen_socket is None:
            raise ScrcpyError("reverse tunnel listener is not ready")
        deadline = time.monotonic() + self.connect_timeout_s
        while time.monotonic() < deadline:
            try:
                conn, _address = self.listen_socket.accept()
                conn.settimeout(None)
                return conn
            except socket.timeout:
                if self.server_process is not None and self.server_process.poll() is not None:
                    raise ScrcpyError("scrcpy server exited before the reverse socket became ready")
        raise ScrcpyError("timed out waiting for scrcpy reverse socket")

    def _cleanup_runtime(self, *, remove_tunnel: bool) -> None:
        self.stop_event.set()
        with self.frame_condition:
            self.frame_condition.notify_all()
        if self.control_client is not None:
            self.control_client.close()
        for sock in (self.video_socket, self.control_socket, self.listen_socket):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.video_socket = None
        self.control_socket = None
        self.control_client = None
        self.listen_socket = None

        process = self.server_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        self.server_process = None
        if self.decode_thread is not None and self.decode_thread.is_alive():
            self.decode_thread.join(timeout=3)
        self.decode_thread = None
        if self.log_thread is not None and self.log_thread.is_alive():
            self.log_thread.join(timeout=1)
        self.log_thread = None
        self.decoder = None
        if remove_tunnel:
            try:
                if self.tunnel_mode == "forward":
                    self.controller.remove_forward(self.local_port)
                elif self.tunnel_mode == "reverse":
                    self.controller.remove_reverse(self.socket_name)
            except ADBError:
                # A disconnected device cannot acknowledge tunnel cleanup;
                # the local sockets and server process are already closed.
                pass
        self.tunnel_mode = None

    @staticmethod
    def _open_listener(port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(2)
        sock.settimeout(1.0)
        return sock

    @staticmethod
    def _pick_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
