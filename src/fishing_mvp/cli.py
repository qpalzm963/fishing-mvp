"""Command-line entrypoints for offline analysis, debug, and live dry-run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .capture import scrcpy_status
from .config import load_config
from .live import run_live
from .pipeline import analyze_video
from .runtime import PortablePaths


def _configure_utf8_stdio() -> None:
    """Make frozen Windows console and pipe output deterministic when possible."""

    if sys.platform != "win32":
        return
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, TypeError, ValueError):
            # Some embedded/frozen hosts expose a stream without a writable
            # encoding. Machine-readable commands still use ASCII JSON below.
            continue


def _portable_tool_path(name: str, explicit: str | None = None) -> str | None:
    """Resolve a bundled native tool when running the frozen application."""

    if explicit:
        return explicit
    paths = PortablePaths.current()
    candidate = getattr(paths, name)
    if getattr(sys, "frozen", False):
        return str(candidate)
    return str(candidate) if candidate.is_file() else None


def _repo_default_config() -> Path | None:
    paths = PortablePaths.current()
    candidates = [
        paths.default_config,
        Path(__file__).resolve().parent / "defaults" / "default.yaml",
    ]
    return next((candidate for candidate in candidates if candidate is not None and candidate.exists()), None)


def _config(path: str | None):
    paths = PortablePaths.current()
    default_path = _repo_default_config()
    override_path = Path(path) if path else (paths.user_config if paths.user_config.exists() else None)
    if default_path is not None or override_path is not None:
        return load_config(override_path, base_path=default_path)
    return load_config()


def _max_rounds(value: str) -> int:
    try:
        rounds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("輪數必須是 1 到 999 的整數。") from exc
    if not 1 <= rounds <= 999:
        raise argparse.ArgumentTypeError("輪數必須介於 1 到 999。")
    return rounds


def _runtime_smoke() -> dict[str, object]:
    """Exercise imports that are intentionally lazy in the live path.

    This command is used by the assembled Windows package smoke test. It
    loads the packaged baseline config and creates an H.264 decoder from the
    frozen executable itself, so a missing PyAV/FFmpeg DLL cannot be hidden by
    a successful build-environment import.
    """

    packaged_config = _repo_default_config()
    if packaged_config is None:
        return {
            "ok": False,
            "error": {
                "code": "config_unavailable",
                "message": "找不到 packaged default.yaml。",
            },
        }
    try:
        load_config(packaged_config)
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "config_unavailable",
                "message": f"無法載入 packaged default.yaml：{exc}",
            },
        }
    try:
        import av

        decoder = av.CodecContext.create("h264", "r")
    except Exception as exc:
        return {
            "ok": False,
            "error": {
                "code": "pyav_h264_unavailable",
                "message": f"無法建立 PyAV H.264 decoder：{exc}",
            },
        }
    return {
        "ok": True,
        "config_loaded": True,
        "codec": "h264",
        "decoder_name": str(getattr(decoder, "name", "h264")),
        "pyav_version": getattr(av, "__version__", None),
    }


def _add_video_args(parser: argparse.ArgumentParser, default_output: str) -> None:
    parser.add_argument("--input", required=True, help="Input video path")
    parser.add_argument("--output-dir", default=default_output, help="Directory for debug artefacts")
    parser.add_argument("--config", help="YAML configuration path")
    parser.add_argument("--analysis-fps", type=float, default=None, help="Detector sampling rate; video output keeps source FPS")
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--no-video", action="store_true", help="Skip annotated MP4 generation")
    parser.add_argument("--full-auto", action="store_true", help="Preview start, QTE, and result actions offline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fishing-mvp", description="Dynamic fishing UI detector with safe ADB control")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze-video", help="Analyse a recorded video offline")
    _add_video_args(analyze, "outputs/video_analysis")

    debug = subparsers.add_parser("debug", help="Offline analysis with annotated debug artefacts")
    _add_video_args(debug, "outputs/debug")

    live = subparsers.add_parser("live", help="Read live device frames; dry-run unless --live is explicit")
    live.add_argument("--serial", required=True, help="ADB device serial")
    live.add_argument("--package", help="Expected foreground package; no package is launched")
    live.add_argument("--config", help="YAML configuration path")
    live.add_argument("--capture", choices=("auto", "scrcpy", "adb"), default="auto", help="Live frame source")
    live.add_argument("--fps", type=float, default=None, help="Detector sampling rate")
    live.add_argument("--qte-fps", type=float, default=None, help="QTE/QUALITY detector sampling rate")
    live.add_argument("--output-dir", default="outputs/live")
    live.add_argument("--max-seconds", type=float, default=None)
    live.add_argument("--live", action="store_true", help="Actually send ADB input; omit for dry-run")
    live.add_argument("--full-auto", action="store_true", help="Enable dynamic start -> QTE -> result automation")
    live.add_argument("--max-rounds", type=_max_rounds, default=None, help="Full-auto rounds before stopping (1-999; default: 1)")
    live.add_argument("--enable-qte", action="store_true", help="Allow QTE tap proposals to be sent")
    live.add_argument("--auto-continue", action="store_true", help="Allow a detected result continue button to be tapped")
    live.add_argument("--adb-path", help="Explicit ADB executable path; frozen builds use the bundled copy")
    live.add_argument("--scrcpy-path", help="Explicit scrcpy executable path; frozen builds use the bundled copy")

    probe = subparsers.add_parser("probe", help="Show local capture/control prerequisites")
    probe.add_argument("--serial", help="Optional ADB serial to inspect")
    probe.add_argument("--adb-path", help="Explicit ADB executable path")
    probe.add_argument("--scrcpy-path", help="Explicit scrcpy executable path")

    subparsers.add_parser(
        "runtime-smoke",
        help="Exercise packaged config and the PyAV H.264 decoder from this executable",
    )

    discover = subparsers.add_parser("discover-device", help="Safely select exactly one authorized ADB device")
    discover.add_argument("--adb-path", help="ADB executable path; frozen builds use the bundled copy")
    discover.add_argument("--json", action="store_true", help="Write one machine-readable JSON object")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"analyze-video", "debug"}:
            config = _config(args.config)
            if args.full_auto:
                config.action.auto_start = True
                config.action.qte_enabled = True
                config.action.auto_continue = True
            summary = analyze_video(
                args.input,
                args.output_dir,
                config,
                analysis_fps=args.analysis_fps,
                max_seconds=args.max_seconds,
                write_video=not args.no_video,
            )
            print(json.dumps({"output_dir": summary["output_dir"], "transitions": summary["transitions"], "actions": summary["actions"]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "live":
            config = _config(args.config)
            if args.fps is not None:
                config.capture_fps = max(0.5, args.fps)
            if args.qte_fps is not None:
                config.qte_capture_fps = max(0.5, args.qte_fps)
            summary = run_live(
                serial=args.serial,
                package=args.package,
                config=config,
                output_dir=args.output_dir,
                capture_mode=args.capture,
                send_actions=args.live,
                qte_enabled=args.enable_qte,
                auto_continue=args.auto_continue,
                max_seconds=args.max_seconds,
                full_auto=args.full_auto,
                max_rounds=args.max_rounds,
                adb_path=_portable_tool_path("adb", args.adb_path) or "adb",
                scrcpy_executable=_portable_tool_path("scrcpy", args.scrcpy_path),
            )
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if args.command == "probe":
            adb_path = _portable_tool_path("adb", args.adb_path) or "adb"
            scrcpy_path = _portable_tool_path("scrcpy", args.scrcpy_path)
            result = {"scrcpy": scrcpy_status(scrcpy_path)}
            if args.serial:
                from .actions import ADBController

                controller = ADBController(args.serial, adb_path=adb_path)
                controller.assert_connected()
                result["serial"] = args.serial
                result["foreground_package"] = controller.foreground_package()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "runtime-smoke":
            payload = _runtime_smoke()
            # Keep this diagnostic safe for direct invocation from a Windows
            # OEM/charmap console. JSON consumers decode the escaped Unicode.
            print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
            return 0 if payload.get("ok") is True else 2
        if args.command == "discover-device":
            from .device_discovery import DeviceDiscovery

            adb_path = _portable_tool_path("adb", args.adb_path) or "adb"
            payload = DeviceDiscovery(adb_path).payload()
            # This output is consumed by portable/START.bat, so it must remain
            # parseable even when Python inherited a Windows charmap stream.
            print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
            return 0 if payload.get("ok") is True else 2
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1
