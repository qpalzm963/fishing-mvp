"""Human-facing console flow for the self-contained Windows package."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import re
import sys
import time
import traceback
from typing import Callable, TextIO
import uuid

from .actions import ADBController
from .config import load_config
from .device_discovery import DeviceDiscovery, DeviceDiscoveryError
from .live import run_live
from .models import FishingState
from .portable_session import PortableSession, SessionBusy, process_matches, read_json, write_json
from .runtime import PortablePaths


STATE_LABELS = {
    FishingState.UNKNOWN: "辨識畫面中",
    FishingState.WAITING: "等待魚上鉤／準備開始",
    FishingState.PROMPT: "準備拉竿",
    FishingState.CASTING: "拉竿動畫中",
    FishingState.QTE: "釣魚操作中",
    FishingState.QUALITY: "命中回饋",
    FishingState.RESULT: "結算中",
    FishingState.ERROR: "畫面異常",
}


class Console:
    def __init__(self, output: TextIO):
        self.output = output
        self.last_progress: tuple[FishingState, int] | None = None
        self.last_update = -10.0

    def say(self, message: str = "") -> None:
        print(message, file=self.output, flush=True)

    def progress(self, state: FishingState, completed: int, target: int | None, elapsed: float) -> None:
        # Show changes and a quiet heartbeat rather than 10–30 raw rows/sec.
        current = (state, completed)
        if current == self.last_progress and elapsed - self.last_update < 5.0:
            return
        self.last_progress = current
        self.last_update = elapsed
        self.say(f"  已完成 {completed} / {target or '-'} 輪  ·  {STATE_LABELS[state]}  ·  {int(elapsed)} 秒")


def ask_rounds(read_input: Callable[[str], str], console: Console) -> int | None:
    while True:
        value = read_input("執行幾輪？1–999，Enter = 1，Q = 離開：").strip()
        if value.lower() == "q":
            return None
        if not value:
            return 1
        if re.fullmatch(r"[0-9]{1,3}", value) and 1 <= int(value) <= 999:
            return int(value)
        console.say("[請重新輸入] 輪數需為 1 到 999 的整數，例如 1 或 10。")


def retry(read_input: Callable[[str], str], console: Console) -> bool:
    while True:
        answer = read_input("處理好後按 Enter 或 R 重試；Q 離開：").strip().lower()
        if answer in {"", "r"}:
            return True
        if answer == "q":
            return False
        console.say("請輸入 R 重試，或 Q 離開。")


def error_guidance(exc: Exception) -> str:
    if isinstance(exc, DeviceDiscoveryError):
        return {
            "no_devices": "找不到手機。請接上可傳輸資料的 USB 線、開啟 USB 偵錯，再重試。",
            "unauthorized": "手機尚未授權。請解鎖手機，按下「允許 USB 偵錯」。",
            "offline": "手機連線離線。請重新插拔 USB 線，確認手機已解鎖。",
            "multiple_authorized_devices": "偵測到多台手機。請只保留要操作的一台，再重試。",
            "mixed_devices": "偵測到多個裝置或連線狀態。請移除其他手機與模擬器，只保留一台已授權手機。",
        }.get(exc.code, "無法確認手機連線。請重新連接手機；詳細原因已寫入診斷紀錄。")
    if isinstance(exc, ValueError):
        return f"設定內容無效：{exc}\n請修正 config\\user.yaml；若未自訂設定，請重新解壓縮完整套件。"
    detail = str(exc).lower()
    if "foreground" in detail:
        return "無法確認遊戲仍在前景。請解鎖手機，回到釣魚畫面後再重試。"
    if "size" in detail or "aspect ratio" in detail:
        return "手機畫面尺寸或方向不符。請固定遊戲方向，回到釣魚畫面後重新開始。"
    if "scrcpy" in detail or "decoder" in detail:
        return "影像串流無法啟動或已中斷。請重新連接手機；若持續失敗，請重新解壓縮完整套件。"
    if isinstance(exc, FileNotFoundError):
        return "套件檔案不完整。請完整解壓縮 ZIP，保留 START.bat 旁的 runtime 與 config 資料夾。"
    return "執行已停止。請確認手機連線與遊戲畫面，再重試；詳細原因已寫入診斷紀錄。"


def _attempt(paths, rounds, session, output_dir, console):
    missing = paths.missing_bundled_files()
    if not paths.default_config.is_file():
        missing.append(paths.default_config)
    if missing:
        raise FileNotFoundError("Missing portable files: " + ", ".join(str(path) for path in missing))
    config = load_config(paths.user_config if paths.user_config.exists() else None, base_path=paths.default_config)
    console.say("[1/3] 套件與設定檢查通過。")
    device = DeviceDiscovery(paths.adb).discover()
    console.say(f"[2/3] 已連接手機：{device.serial}")
    controller = ADBController(device.serial, adb_path=paths.adb)
    package = controller.foreground_package()
    if not package or not re.fullmatch(r"[A-Za-z0-9._]+", package):
        raise RuntimeError("Foreground package is unavailable")
    console.say(f"[3/3] 已確認前景 App：{package}")
    console.say(f"\n開始自動操作，共 {rounds} 輪。請保持遊戲在前景。")
    console.say("需要停止：雙擊 STOP.bat，或在此視窗按 Ctrl+C。")
    console.say("正在連接遊戲畫面，請稍候…")
    console.last_progress = None
    console.last_update = -10.0
    try:
        session.begin(output_dir)
        # Keep raw per-frame output and exceptions out of the operator view.
        # Console owns the original stdout, so progress remains visible here.
        with (output_dir / "diagnostics.log").open("a", encoding="utf-8") as log:
            with redirect_stdout(log), redirect_stderr(log):
                return run_live(
                    serial=device.serial, package=package, config=config,
                    output_dir=output_dir, send_actions=True, full_auto=True,
                    max_rounds=rounds, capture_mode="scrcpy",
                    adb_path=str(paths.adb), scrcpy_executable=str(paths.scrcpy),
                    stop_requested=session.stop_requested, on_progress=console.progress,
                )
    finally:
        session.finish()


def start_console(paths: PortablePaths, *, read_input=None, output: TextIO | None = None) -> int:
    read_input = read_input or input
    console = Console(output or sys.stdout)
    console.say("\n  Fishing MVP · 自動釣魚\n  ──────────────────────────────")
    console.say("  1. 接上手機，只保留一台裝置。")
    console.say("  2. 開啟 USB 偵錯，並在手機允許授權。")
    console.say("  3. 開啟遊戲，停在釣魚畫面。\n")
    try:
        with PortableSession(paths.root / "run") as session:
            rounds = ask_rounds(read_input, console)
            if rounds is None:
                console.say("已取消，未開始操作。")
                return 0
            while True:
                output_dir = session.root / "sessions" / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
                output_dir.mkdir(parents=True)
                try:
                    summary = _attempt(paths, rounds, session, output_dir, console)
                except Exception as exc:
                    with (output_dir / "diagnostics.log").open("a", encoding="utf-8") as log:
                        traceback.print_exc(file=log)
                    summary = read_json(output_dir / "live_summary.json")
                    console.say(f"\n[未完成] 已完成 {summary.get('completed_rounds', 0)} / {rounds} 輪。")
                    console.say(error_guidance(exc))
                    console.say(f"診斷紀錄：{output_dir}")
                    console.say(f"重試會重新檢查手機，並開始新的 {rounds} 輪。")
                    if retry(read_input, console):
                        continue
                    return 2
                reason = summary.get("stop_reason") or "unknown"
                completed = summary.get("completed_rounds", 0)
                retry_summary = summary.get("retry") or {}
                console.say()
                if reason == "completed_rounds":
                    console.say(f"[完成] 已完成 {completed} / {rounds} 輪。")
                elif reason in {"user_stop", "keyboard_interrupt"}:
                    console.say(f"[已停止] 已完成 {completed} / {rounds} 輪，執行紀錄已保存。")
                elif reason == "start_unconfirmed" or (
                    reason == "max_retry_attempts" and retry_summary.get("last_failure") == "start_unconfirmed"
                ):
                    console.say(f"[未完成] 已完成 {completed} / {rounds} 輪。開始點擊已送出，但畫面未進入釣魚流程。")
                    if reason == "max_retry_attempts":
                        console.say("自動重試額度已用完。")
                    console.say("請確認遊戲停在釣魚畫面，再重新開始。")
                else:
                    state = reason.partition(":")[2]
                    label = next((label for key, label in STATE_LABELS.items() if key.value == state), "目前階段")
                    console.say(f"[未完成] 已完成 {completed} / {rounds} 輪。{label}等待逾時或流程已中止。")
                    console.say("請確認遊戲停在釣魚畫面，再重新開始。")
                console.say(f"執行紀錄：{output_dir}")
                if summary.get("warning_counts"):
                    console.say("本次有診斷警告，詳細內容請查看執行紀錄。")
                if reason == "completed_rounds" or reason in {"user_stop", "keyboard_interrupt"}:
                    return 0
                console.say(f"重試會開始新的 {rounds} 輪。")
                if not retry(read_input, console):
                    return 2
    except SessionBusy as exc:
        console.say(f"[已有執行視窗] {exc}")
        return 2
    except (KeyboardInterrupt, EOFError):
        console.say("\n已取消。")
        return 0
    except OSError:
        console.say("[無法存取檔案] 請將完整套件解壓縮到可寫入的資料夾，例如桌面，再重新執行。")
        return 2


def stop_console(paths: PortablePaths, *, output: TextIO | None = None) -> int:
    console = Console(output or sys.stdout)
    record_path = paths.root / "run" / "session.json"
    record = read_json(record_path)
    if not record:
        console.say("目前沒有正在自動操作的工作。若啟動視窗正在等待輸入，請回到該視窗輸入 Q。")
        return 0
    token = record.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token) or not process_matches(
        record.get("pid"), paths.runtime_dir / "FishingMVP.exe"
    ):
        console.say("無法確認這個資料夾的執行程序，未送出停止指令。請回到原視窗按 Ctrl+C。")
        return 2
    try:
        write_json(paths.root / "run" / "stop.request", {"token": token})
    except OSError:
        console.say("無法寫入停止要求。請回到原視窗按 Ctrl+C。")
        return 2
    console.say("已送出停止要求，正在等待停止輸入、保存紀錄與關閉連線…")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if read_json(record_path).get("token") != token:
            console.say("[已停止] 請回到原啟動視窗查看完成輪數與結果。")
            return 0
        time.sleep(0.2)
    console.say("停止要求仍有效，程式可能正在等待連線回應。請查看原視窗；需要時可按 Ctrl+C。")
    return 2
