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


def _repo_default_config() -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
    return candidate if candidate.exists() else None


def _config(path: str | None):
    return load_config(path or _repo_default_config())


def _add_video_args(parser: argparse.ArgumentParser, default_output: str) -> None:
    parser.add_argument("--input", required=True, help="Input video path")
    parser.add_argument("--output-dir", default=default_output, help="Directory for debug artefacts")
    parser.add_argument("--config", help="YAML configuration path")
    parser.add_argument("--analysis-fps", type=float, default=None, help="Detector sampling rate; video output keeps source FPS")
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--no-video", action="store_true", help="Skip annotated MP4 generation")


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
    live.add_argument("--output-dir", default="outputs/live")
    live.add_argument("--max-seconds", type=float, default=None)
    live.add_argument("--live", action="store_true", help="Actually send ADB input; omit for dry-run")
    live.add_argument("--enable-qte", action="store_true", help="Allow QTE tap proposals to be sent")
    live.add_argument("--auto-continue", action="store_true", help="Allow a detected result continue button to be tapped")

    probe = subparsers.add_parser("probe", help="Show local capture/control prerequisites")
    probe.add_argument("--serial", help="Optional ADB serial to inspect")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"analyze-video", "debug"}:
            config = _config(args.config)
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
            )
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if args.command == "probe":
            result = {"scrcpy": scrcpy_status()}
            if args.serial:
                from .actions import ADBController

                controller = ADBController(args.serial)
                controller.assert_connected()
                result["serial"] = args.serial
                result["foreground_package"] = controller.foreground_package()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1
