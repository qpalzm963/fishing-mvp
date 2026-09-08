"""Offline video analysis and reproducible debug artefact generation."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

import cv2
import yaml

from .capture import VideoFrameSource
from .config import AppConfig
from .debug import draw_overlay, save_snapshot
from .models import Action, FishingState, StateTransition
from .state_machine import ActionPlanner, FishingStateMachine
from .vision import FrameAnalyzer


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _open_writer(path: Path, fps: float, width: int, height: int) -> cv2.VideoWriter:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"Unable to open annotated video writer: {path}")
    return writer


def analyze_video(
    input_path: str | Path,
    output_dir: str | Path,
    config: AppConfig,
    analysis_fps: float | None = None,
    max_seconds: float | None = None,
    write_video: bool = True,
) -> dict[str, Any]:
    """Analyse a video and write JSONL, CSV, summary, snapshots, and overlay video."""

    input_path = Path(input_path).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = output_dir / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    capture = cv2.VideoCapture(str(input_path))
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open video: {input_path}")
    source_fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0)
    source_fps = source_fps if source_fps > 0 else 30.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_count_meta = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    capture.release()

    target_analysis_fps = max(1.0, analysis_fps or config.capture_fps)
    analysis_interval = 1.0 / target_analysis_fps
    writer = _open_writer(output_dir / "annotated.mp4", source_fps, width, height) if write_video else None
    analyzer = FrameAnalyzer(config.detector)
    state_machine = FishingStateMachine(config.state_machine)
    planner = ActionPlanner(config.action)
    states = Counter()
    hints = Counter()
    transitions: list[StateTransition] = []
    actions: list[dict[str, Any]] = []
    detection_path = output_dir / "detections.jsonl"
    timeline_path = output_dir / "timeline.csv"
    source = VideoFrameSource(str(input_path), max_seconds=max_seconds)
    next_analysis = -1.0
    last_detection = None
    last_state = FishingState.UNKNOWN
    frame_count = 0
    analysed_frames = 0
    snapshot_count = 0

    with detection_path.open("w", encoding="utf-8") as detections_file, timeline_path.open("w", encoding="utf-8", newline="") as timeline_file:
        timeline_writer = csv.DictWriter(
            timeline_file,
            fieldnames=["frame_index", "timestamp_s", "state", "hint", "confidence", "analysed", "action"],
        )
        timeline_writer.writeheader()
        try:
            for packet in source:
                analysed = last_detection is None or packet.timestamp_s + 1e-5 >= next_analysis
                action: Action | None = None
                transition = None
                if analysed:
                    detection = analyzer.analyze(packet.frame, packet.frame_index, packet.timestamp_s)
                    current_state, transition = state_machine.update(detection)
                    action = planner.plan(detection, current_state, transition)
                    last_detection = detection
                    last_state = current_state
                    next_analysis = packet.timestamp_s + analysis_interval
                    analysed_frames += 1
                    hints[detection.hint.value] += 1
                    if transition is not None:
                        transitions.append(transition)
                        snapshot = draw_overlay(packet.frame, detection, current_state, action, analysed=True)
                        save_snapshot(snapshot, snapshot_dir / f"{len(transitions):03d}_{transition.to_state.value}_{packet.timestamp_s:07.2f}.jpg")
                        snapshot_count += 1
                else:
                    detection = last_detection

                if detection is None:
                    continue
                states[last_state.value] += 1
                if action is not None:
                    action_dict = {"frame_index": packet.frame_index, "timestamp_s": packet.timestamp_s, **action.to_dict()}
                    actions.append(action_dict)
                line = detection.to_dict()
                line.update({"state": last_state.value, "analysed": analysed, "action": action.to_dict() if action else None})
                detections_file.write(json.dumps(line, ensure_ascii=False) + "\n")
                timeline_writer.writerow(
                    {
                        "frame_index": packet.frame_index,
                        "timestamp_s": f"{packet.timestamp_s:.4f}",
                        "state": last_state.value,
                        "hint": detection.hint.value,
                        "confidence": f"{detection.confidence:.4f}",
                        "analysed": analysed,
                        "action": action.action_type.value if action else "",
                    }
                )
                if writer is not None:
                    writer.write(draw_overlay(packet.frame, detection, last_state, action, analysed=analysed))
                frame_count += 1
        finally:
            if writer is not None:
                writer.release()

    summary: dict[str, Any] = {
        "input": str(input_path),
        "output_dir": str(output_dir),
        "video": {
            "width": width,
            "height": height,
            "source_fps": source_fps,
            "metadata_frame_count": frame_count_meta,
            "processed_frame_count": frame_count,
            "analysed_frame_count": analysed_frames,
            "duration_s": round(frame_count / source_fps, 4) if source_fps else None,
        },
        "config": config.to_dict(),
        "state_counts": dict(states),
        "hint_counts": dict(hints),
        "transitions": [item.to_dict() for item in transitions],
        "actions": actions,
        "debug": {
            "annotated_video": str(output_dir / "annotated.mp4") if write_video else None,
            "detections_jsonl": str(detection_path),
            "timeline_csv": str(timeline_path),
            "snapshot_count": snapshot_count,
        },
        "notes": [
            "Coordinates are derived from per-frame detections and emitted as normalized values.",
            "Action proposals are not executed during offline analysis.",
        ],
    }
    _write_json(output_dir / "summary.json", summary)
    (output_dir / "config_resolved.yaml").write_text(
        yaml.safe_dump(config.to_dict(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return summary
