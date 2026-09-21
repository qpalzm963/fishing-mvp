"""Exclusive portable sessions and scoped, cooperative stop requests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import uuid
from typing import Any


class SessionBusy(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def process_matches(pid: int, executable: Path) -> bool:
    """Query the actual process image; never trust a stale PID on its own."""
    if type(pid) is not int or pid <= 0:
        return False
    if os.name != "nt":
        return pid == os.getpid() and executable.resolve() == Path(sys.executable).resolve()
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return False
        return os.path.normcase(os.path.realpath(buffer.value)) == os.path.normcase(os.path.realpath(executable))
    finally:
        kernel.CloseHandle(handle)


class PortableSession:
    def __init__(self, run_root: Path):
        self.root = run_root
        self.handle = None
        self.token: str | None = None

    def __enter__(self) -> "PortableSession":
        self.root.mkdir(parents=True, exist_ok=True)
        self.handle = (self.root / "session.lock").open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise SessionBusy("這個資料夾已有啟動視窗，請回到原視窗操作；需要停止時請雙擊 STOP.bat。") from exc
        # Only the owner of the lock may discard stale records.
        try:
            self.finish()
        except OSError:
            self.handle.close()
            self.handle = None
            raise
        return self

    def begin(self, output_dir: Path) -> None:
        self.token = uuid.uuid4().hex
        write_json(self.root / "session.json", {
            "pid": os.getpid(), "token": self.token,
            "executable": str(Path(sys.executable).resolve()),
            "output_dir": str(output_dir),
        })
        (self.root / "FishingMVP.pid").write_text(str(os.getpid()), encoding="ascii")

    def stop_requested(self) -> bool:
        return self.token is not None and read_json(self.root / "stop.request").get("token") == self.token

    def finish(self) -> None:
        for name in ("session.json", "FishingMVP.pid", "stop.request"):
            (self.root / name).unlink(missing_ok=True)
        self.token = None

    def __exit__(self, *args: object) -> None:
        try:
            self.finish()
        finally:
            if self.handle is not None:
                # Closing releases the OS lock even after a crash; never unlink
                # the lock file while another process may hold an open handle.
                self.handle.close()
                self.handle = None
