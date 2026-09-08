"""Conservative ADB live runner.  It never launches or restarts an App."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .actions import ADBController
from .capture import LiveFrameSource, create_live_frame_source, scrcpy_status
from .config import AppConfig
from .debug import draw_overlay, save_snapshot
from .models import Action
from .state_machine import ActionPlanner, AutomationProgress, FishingStateMachine
from .vision import FrameAnalyzer


def _map_action_to_device(
    action: Action,
    frame_size: tuple[int, int],
    device_size: tuple[int, int],
) -> Action:
    """Map normalized detector coordinates to the device framebuffer."""

    if action.x_norm is None or action.y_norm is None:
        return action
    frame_width, frame_height = frame_size
    device_width, device_height = device_size
    if frame_width <= 0 or frame_height <= 0 or device_width <= 0 or device_height <= 0:
        return action
    if frame_size == device_size:
        return action
    frame_ratio = frame_width / float(frame_height)
    device_ratio = device_width / float(device_height)
    ratio_error = abs(frame_ratio - device_ratio) / max(frame_ratio, device_ratio)
    if ratio_error > 0.03:
        raise RuntimeError(
            "Frame/device aspect ratio mismatch; refusing to send coordinates: "
            f"frame={frame_width}x{frame_height}, device={device_width}x{device_height}"
        )
    return replace(
        action,
        x=min(device_width - 1, max(0, round(action.x_norm * device_width))),
        y=min(device_height - 1, max(0, round(action.y_norm * device_height))),
    )


def run_live(
    serial: str,
    package: str | None,
    config: AppConfig,
    output_dir: str | Path,
    send_actions: bool = False,
    qte_enabled: bool = False,
    auto_continue: bool = False,
    max_seconds: float | None = None,
    capture_mode: str = "auto",
    full_auto: bool = False,
    max_rounds: int | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "snapshots").mkdir(parents=True, exist_ok=True)
    if max_rounds is not None and max_rounds < 1:
        raise ValueError("max_rounds must be at least 1")
    effective_qte = bool(qte_enabled or full_auto)
    effective_continue = bool(auto_continue or full_auto)
    config.action.auto_start = bool(full_auto)
    config.action.qte_enabled = effective_qte
    config.action.auto_continue = effective_continue
    controller = ADBController(serial)
    controller.assert_connected()
    if package is not None:
        foreground = controller.foreground_package()
        if foreground != package:
            raise RuntimeError(f"Foreground package mismatch: expected={package}, actual={foreground}")

    source, fallback_note = create_live_frame_source(
        controller,
        mode=capture_mode,
        fps=config.capture_fps,
        scrcpy_max_size=config.scrcpy.max_size,
        scrcpy_max_fps=config.scrcpy.max_fps,
        scrcpy_video_bit_rate=config.scrcpy.video_bit_rate,
        scrcpy_connect_timeout_s=config.scrcpy.connect_timeout_s,
        scrcpy_frame_timeout_s=config.scrcpy.frame_timeout_s,
    )
    analyzer = FrameAnalyzer(config.detector)
    machine = FishingStateMachine(config.state_machine)
    planner = ActionPlanner(config.action)
    target_rounds = (max_rounds or 1) if full_auto else None
    automation = AutomationProgress(max_rounds=target_rounds or 1) if full_auto else None
    start = time.monotonic()
    next_frame = start
    foreground_checked_at = start
    first_size: tuple[int, int] | None = None
    frame_index = 0
    transitions: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    state_counts: dict[str, int] = {}
    log_path = output_dir / "live_detections.jsonl"
    device_size = None
    stop_reason: str | None = None
    if fallback_note:
        print(f"capture: {fallback_note}")
    try:
        device_size = controller.display_size()
        with log_path.open("w", encoding="utf-8") as log:
            try:
                while True:
                    if max_seconds is not None and time.monotonic() - start > max_seconds:
                        stop_reason = "max_seconds"
                        break
                    now = time.monotonic()
                    if now < next_frame:
                        time.sleep(min(0.02, next_frame - now))
                        continue
                    if package is not None:
                        check_interval = max(0.0, config.automation.foreground_check_interval_s)
                        if time.monotonic() - foreground_checked_at >= check_interval:
                            if controller.foreground_package() != package:
                                raise RuntimeError("Foreground package changed; stopping before sending action")
                            foreground_checked_at = time.monotonic()
                    frame = source.read()
                    if first_size is None:
                        first_size = (frame.shape[1], frame.shape[0])
                    if first_size != (frame.shape[1], frame.shape[0]):
                        raise RuntimeError(f"Screen size changed during live run: {first_size} -> {(frame.shape[1], frame.shape[0])}")
                    timestamp_s = time.monotonic() - start
                    detection = analyzer.analyze(frame, frame_index, timestamp_s)
                    state, transition = machine.update(detection)
                    if automation is not None:
                        automation.observe(state, timestamp_s, transition)
                        if automation.timed_out(timestamp_s, config.automation):
                            automation.stop_reason = f"state_timeout:{state.value}"
                    action = None if automation is not None and automation.stop_reason is not None else planner.plan(
                        detection,
                        state,
                        transition,
                        start_allowed=automation.start_allowed if automation is not None else True,
                    )
                    state_counts[state.value] = state_counts.get(state.value, 0) + 1
                    record: dict[str, Any] = {
                        "state": state.value,
                        "detection": detection.to_dict(),
                        "action": action.to_dict() if action else None,
                    }
                    if transition is not None:
                        transition_dict = transition.to_dict()
                        transitions.append(transition_dict)
                        record["transition"] = transition_dict
                        save_snapshot(draw_overlay(frame, detection, state, action), output_dir / "snapshots" / f"{len(transitions):03d}_{state.value}_{timestamp_s:07.2f}.jpg")
                    if action is not None:
                        if send_actions and device_size is None and action.x_norm is not None:
                            raise RuntimeError("Device display size is unavailable; refusing to send normalized coordinates")
                        sent_action = _map_action_to_device(
                            action,
                            (frame.shape[1], frame.shape[0]),
                            device_size or (frame.shape[1], frame.shape[0]),
                        )
                        action_execution_ms: float | None = None
                        if send_actions:
                            execution_started = time.monotonic()
                            controller.execute(sent_action)
                            action_execution_ms = (time.monotonic() - execution_started) * 1000.0
                            record["action_sent"] = True
                            record["sent_action"] = sent_action.to_dict()
                            record["action_execution_ms"] = round(action_execution_ms, 2)
                        else:
                            record["action_sent"] = False
                        action_record = {
                            "timestamp_s": timestamp_s,
                            **action.to_dict(),
                            "sent": send_actions,
                            "sent_action": sent_action.to_dict() if send_actions else None,
                        }
                        if action_execution_ms is not None:
                            action_record["execution_ms"] = round(action_execution_ms, 2)
                        actions.append(action_record)
                    log.write(json.dumps(record, ensure_ascii=False) + "\n")
                    log.flush()
                    print(f"t={timestamp_s:7.2f}s state={state.value:8s} conf={detection.confidence:.2f} action={'sent' if send_actions and action else 'proposal' if action else '-'}")
                    frame_index += 1
                    if automation is not None and automation.stop_reason is not None:
                        stop_reason = automation.stop_reason
                        break
                    next_frame = max(next_frame + 1.0 / max(0.5, config.capture_fps), time.monotonic())
            except KeyboardInterrupt:
                stop_reason = "keyboard_interrupt"
    finally:
        source.close()
    summary = {
        "serial": serial,
        "package": package,
        "capture_mode_requested": capture_mode,
        "capture": source.info(),
        "send_actions": send_actions,
        "full_auto": full_auto,
        "max_rounds": target_rounds,
        "completed_rounds": automation.completed_rounds if automation is not None else 0,
        "stop_reason": stop_reason or (automation.stop_reason if automation is not None else None),
        "auto_start": full_auto,
        "qte_enabled": effective_qte,
        "auto_continue": effective_continue,
        "frame_count": frame_index,
        "state_counts": state_counts,
        "transitions": transitions,
        "actions": actions,
        "scrcpy": scrcpy_status(),
        "log": str(log_path),
        "notes": [
            "Live runner stops on ADB errors, foreground changes, or screen-size changes.",
            "Foreground package checks are periodic so they do not block every QTE input.",
            "Full-auto remains dry-run unless --live is explicitly supplied.",
            "Full-auto stops after the configured number of RESULT-seen then WAITING round completions or a stage timeout.",
            *([fallback_note] if fallback_note else []),
        ],
    }
    (output_dir / "live_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary
