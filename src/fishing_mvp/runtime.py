"""Executable-relative paths used by the Windows portable distribution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


@dataclass(frozen=True)
class PortablePaths:
    """The stable directory contract for the onedir portable package."""

    root: Path
    executable_dir: Path
    runtime_dir: Path
    scrcpy_dir: Path
    adb: Path
    scrcpy: Path
    scrcpy_server: Path
    config_dir: Path
    default_config: Path
    user_config: Path

    @classmethod
    def from_root(cls, root: str | Path, *, executable_dir: str | Path | None = None) -> "PortablePaths":
        package_root = Path(root).expanduser().resolve()
        executable_parent = Path(executable_dir).expanduser().resolve() if executable_dir is not None else package_root / "runtime"
        runtime_dir = package_root / "runtime"
        scrcpy_dir = runtime_dir / "scrcpy"
        return cls(
            root=package_root,
            executable_dir=executable_parent,
            runtime_dir=runtime_dir,
            scrcpy_dir=scrcpy_dir,
            adb=scrcpy_dir / "adb.exe",
            scrcpy=scrcpy_dir / "scrcpy.exe",
            scrcpy_server=scrcpy_dir / "scrcpy-server",
            config_dir=package_root / "config",
            default_config=package_root / "config" / "default.yaml",
            user_config=package_root / "config" / "user.yaml",
        )

    @classmethod
    def current(cls) -> "PortablePaths":
        """Resolve the package root for source and frozen execution."""

        if getattr(sys, "frozen", False):
            executable_dir = Path(sys.executable).resolve().parent
            root = executable_dir.parent if executable_dir.name.casefold() == "runtime" else executable_dir
            return cls.from_root(root, executable_dir=executable_dir)
        root = Path(__file__).resolve().parents[2]
        return cls.from_root(root, executable_dir=root)

    def missing_bundled_files(self) -> list[Path]:
        """Return missing Windows runtime files without raising or guessing."""

        return [path for path in (self.adb, self.scrcpy, self.scrcpy_server) if not path.is_file()]
