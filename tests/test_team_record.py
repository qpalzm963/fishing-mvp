"""Team-record banner: count it once per catch and keep the snapshot.

The detector is checked against real phone frames from a live session
(2026-11-05, 720px capture). The first version flagged every round: the
「恭喜獲得」 reward screen and a frame that had already returned to the start
screen both passed. Those frames are kept here so that cannot come back.
"""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from fishing_mvp.mobile import MobileSession
from fishing_mvp.models import FishingState
from fishing_mvp.vision import detect_team_record_banner

FIXTURES = Path(__file__).parent / "fixtures"
RESULT = FishingState.RESULT


def _load(name: str) -> np.ndarray:
    frame = cv2.imread(str(FIXTURES / name))
    assert frame is not None, name
    return frame


@pytest.mark.parametrize("name", [
    "team_record_banner_large.jpg",   # banner just appeared, glyphs run past the edges
    "team_record_banner_small.jpg",   # banner shrunk, reward coins flying over the card
    "team_record_banner_dog.jpg",     # large again, first glyph off screen
    "team_record_banner_magnet.jpg",  # small, over the faded start screen
])
@pytest.mark.parametrize("width", [540, 720, 1080])
def test_real_team_record_banner_is_detected(name: str, width: int) -> None:
    frame = _load(name)
    height = round(frame.shape[0] * width / frame.shape[1])
    assert detect_team_record_banner(cv2.resize(frame, (width, height)))


@pytest.mark.parametrize("width", [540, 720, 1080])
def test_personal_record_banner_is_not_a_team_record(width: int) -> None:
    # 「刷新個人紀錄」 shares the lettering; only the team record is counted.
    frame = _load("personal_record_banner.jpg")
    height = round(frame.shape[0] * width / frame.shape[1])
    assert not detect_team_record_banner(cv2.resize(frame, (width, height)))


@pytest.mark.parametrize("name", [
    "reward_congrats.jpg",     # 「恭喜獲得」: yellow lettering, no catch card
    "result_lag_waiting.jpg",  # start screen while the state machine still said RESULT
    "result_no_banner.jpg",    # an ordinary catch card
    "result.jpg", "waiting.jpg", "prompt.jpg", "qte.jpg", "quality.jpg",
    "cool_20260921.jpg", "qte_splash_16s.jpg", "qte_splash_20s.jpg", "qte_splash_21s.jpg",
])
def test_frames_without_the_banner_are_not_team_records(name: str) -> None:
    assert not detect_team_record_banner(_load(name))


def test_one_catch_records_a_single_snapshot_and_log_line(tmp_path: Path) -> None:
    session = MobileSession(tmp_path / "session")
    frame = _load("team_record_banner_large.jpg")
    try:
        assert session._observe_team_record(frame, RESULT, RESULT, 12.5)
        assert not session._observe_team_record(frame, RESULT, RESULT, 13.0)
        assert not session._observe_team_record(frame, FishingState.WAITING, FishingState.WAITING, 14.0)
        assert session._observe_team_record(frame, RESULT, RESULT, 20.0)
    finally:
        session.stop()
    assert [item["round"] for item in session.team_records] == [1, 1]
    snapshots = list((tmp_path / "session" / "snapshots").glob("team_record_*.jpg"))
    assert len(snapshots) == 2
    lines = (tmp_path / "team_records.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["session"] for line in lines] == ["session", "session"]


def test_result_flicker_does_not_count_the_same_banner_twice(tmp_path: Path) -> None:
    session = MobileSession(tmp_path / "session")
    frame = _load("team_record_banner_large.jpg")
    try:
        assert session._observe_team_record(frame, RESULT, RESULT, 12.5)
        assert not session._observe_team_record(frame, FishingState.UNKNOWN, FishingState.UNKNOWN, 12.6)
        assert not session._observe_team_record(frame, RESULT, RESULT, 12.7)
    finally:
        session.stop()
    assert len(session.team_records) == 1


def test_frame_that_no_longer_reads_as_result_is_not_checked(tmp_path: Path) -> None:
    # Live run: the state machine still held RESULT while this frame read as casting.
    session = MobileSession(tmp_path / "session")
    frame = _load("team_record_banner_large.jpg")
    try:
        assert not session._observe_team_record(frame, RESULT, FishingState.CASTING, 11.8)
    finally:
        session.stop()
    assert session.team_records == []
