"""Conservative ADB live runner.  It never launches or restarts an App."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import cv2

from .actions import ADBController
from .capture import ADBFrameSource, scrcpy_status
from .config import AppConfig
from .debug import draw_overlay, save_snapshot
from .state_machine import ActionPlanner, FishingStateMachine
from .vision import FrameAnalyzer


def run_live(
    serial: str,
    package: str | None,
    config: AppConfig,
    output_dir: str | Path,
    send_actions: bool = False,
    qte_enabled: bool = False,
    auto_continue: bool = False,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    config.action.qte_enabled = qte_enabled
    config.action.auto_continue = auto_continue
    controller = ADBController(serial)
    controller.assert_connected()
    if package is not None:
        foreground = controller.foreground_package()
        if foreground != package:
            raise RuntimeError(f"Foreground package mismatch: expected={package}, actual={foreground}")

    source = ADBFrameSource(controller, config.capture_fps)
    analyzer = FrameAnalyzer(config.detector)
    machine = FishingStateMachine(config.state_machine)
    planner = ActionPlanner(config.action)
    start = time.monotonic()
    next_frame = start
    first_size: tuple[int, int] | None = None
    frame_index = 0
    transitions: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    state_counts: dict[str, int] = {}
    log_path = output_dir / "live_detections.jsonl"
    with log_path.open("w", encoding="utf-8") as log:
        try:
            while max_seconds is None or time.monotonic() - start <= max_seconds:
                now = time.monotonic()
                if now < next_frame:
                    time.sleep(min(0.02, next_frame - now))
                frame = source.read()
                if first_size is None:
                    first_size = (frame.shape[1], frame.shape[0])
                if first_size != (frame.shape[1], frame.shape[0]):
                    raise RuntimeError(f"Screen size changed during live run: {first_size} -> {(frame.shape[1], frame.shape[0])}")
                timestamp_s = time.monotonic() - start
                detection = analyzer.analyze(frame, frame_index, timestamp_s)
                state, transition = machine.update(detection)
                action = planner.plan(detection, state, transition)
                state_counts[state.value] = state_counts.get(state.value, 0) + 1
                record: dict[str, Any] = {"state": state.value, "detection": detection.to_dict(), "action": action.to_dict() if action else None}
                if transition is not None:
                    transition_dict = transition.to_dict()
                    transitions.append(transition_dict)
                    record["transition"] = transition_dict
                    save_snapshot(draw_overlay(frame, detection, state, action), output_dir / "snapshots" / f"{len(transitions):03d}_{state.value}_{timestamp_s:07.2f}.jpg")
                if action is not None:
                    if send_actions:
                        if package is not None and controller.foreground_package() != package:
                            raise RuntimeError("Foreground package changed; stopping before sending action")
                        controller.execute(action)
                        record["action_sent"] = True
                    else:
                        record["action_sent"] = False
                    actions.append({"timestamp_s": timestamp_s, **action.to_dict(), "sent": send_actions})
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
                log.flush()
                print(f"t={timestamp_s:7.2f}s state={state.value:8s} conf={detection.confidence:.2f} action={'sent' if send_actions and action else 'proposal' if action else '-'}")
                frame_index += 1
                next_frame = max(next_frame + 1.0 / max(0.5, config.capture_fps), time.monotonic())
        except KeyboardInterrupt:
            pass
    summary = {
        "serial": serial,
        "package": package,
        "send_actions": send_actions,
        "qte_enabled": qte_enabled,
        "auto_continue": auto_continue,
        "frame_count": frame_index,
        "state_counts": state_counts,
        "transitions": transitions,
        "actions": actions,
        "scrcpy": scrcpy_status(),
        "log": str(log_path),
        "notes": ["Live runner stops on ADB errors, foreground changes, or screen-size changes."],
    }
    (output_dir / "live_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary
