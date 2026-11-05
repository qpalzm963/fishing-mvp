"""Team-record banner: count it once per catch and keep the snapshot."""

import json
from pathlib import Path

import cv2
import numpy as np

from fishing_mvp.mobile import MobileSession
from fishing_mvp.models import FishingState
from fishing_mvp.vision import detect_team_record_banner


def _frame() -> np.ndarray:
    frame = np.zeros((700, 400, 3), dtype=np.uint8)
    frame[:, :] = (40, 50, 30)
    return frame


def _glyphs(frame: np.ndarray) -> None:
    for index, x in enumerate((70, 130, 190, 250)):
        cv2.rectangle(frame, (x, 110), (x + 36, 168), (20, 190, 230), -1)


def _card(frame: np.ndarray) -> None:
    cv2.rectangle(frame, (80, 230), (320, 420), (220, 180, 40), -1)


def test_banner_on_a_catch_card_is_a_team_record() -> None:
    frame = _frame()
    _glyphs(frame)
    _card(frame)
    assert detect_team_record_banner(frame)


def test_reward_screen_without_a_catch_card_is_not_a_team_record() -> None:
    frame = _frame()
    _glyphs(frame)
    assert not detect_team_record_banner(frame)


def test_sparkle_on_a_catch_card_is_not_a_team_record() -> None:
    frame = _frame()
    _card(frame)
    cv2.rectangle(frame, (180, 90), (300, 230), (20, 210, 240), -1)
    assert not detect_team_record_banner(frame)


def test_one_catch_records_a_single_snapshot_and_log_line(tmp_path: Path) -> None:
    session = MobileSession(tmp_path / "session")
    frame = _frame()
    _glyphs(frame)
    _card(frame)
    try:
        assert session._observe_team_record(frame, FishingState.RESULT, 12.5)
        assert not session._observe_team_record(frame, FishingState.RESULT, 13.0)
        assert not session._observe_team_record(frame, FishingState.WAITING, 14.0)
        assert session._observe_team_record(frame, FishingState.RESULT, 20.0)
    finally:
        session.stop()
    assert [item["round"] for item in session.team_records] == [1, 1]
    snapshots = list((tmp_path / "session" / "snapshots").glob("team_record_*.jpg"))
    assert len(snapshots) == 2
    lines = (tmp_path / "team_records.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["session"] for line in lines] == ["session", "session"]


def test_result_flicker_does_not_count_the_same_banner_twice(tmp_path: Path) -> None:
    session = MobileSession(tmp_path / "session")
    frame = _frame()
    _glyphs(frame)
    _card(frame)
    try:
        assert session._observe_team_record(frame, FishingState.RESULT, 12.5)
        assert not session._observe_team_record(frame, FishingState.UNKNOWN, 12.6)
        assert not session._observe_team_record(frame, FishingState.RESULT, 12.7)
    finally:
        session.stop()
    assert len(session.team_records) == 1
