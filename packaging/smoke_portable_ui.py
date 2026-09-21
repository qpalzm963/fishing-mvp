"""Exercise frozen console entrypoints and actual .bat wrappers on Windows."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def run(command: list[str], answers: str, expected_code: int, expected_text: str) -> None:
    result = subprocess.run(command, input=answers, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=45)
    if result.returncode != expected_code or expected_text not in result.stdout:
        raise RuntimeError(
            f"Portable UI smoke failed: {command!r}\n"
            f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    print(f"PASS: {Path(command[0]).name} ({expected_text})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("package_root", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("Portable UI smoke requires Windows")
    root = args.package_root.resolve()
    if (root / "run").exists():
        raise RuntimeError("Smoke requires a fresh package without existing run records")
    executable = root / "runtime" / "FishingMVP.exe"
    run([str(executable), "portable-start"], "q\n", 0, "已取消")
    # The preceding frozen discovery smoke guarantees a device-free CI runner.
    run([str(executable), "portable-start"], "0\n\nq\n", 2, "找不到手機")
    if (root / "run/session.json").exists():
        raise RuntimeError("Cancelled/failed UI left an active session record")
    run([str(executable), "portable-stop"], "", 0, "沒有正在")
    cmd = os.environ.get("COMSPEC", "cmd.exe")
    run([cmd, "/d", "/c", str(root / "START.bat")], "q\n\n", 0, "按任意鍵關閉")
    run([cmd, "/d", "/c", str(root / "STOP.bat")], "\n", 0, "按任意鍵關閉")
    # Check the bootstrap failure path with spaces/non-ASCII in the folder.
    with tempfile.TemporaryDirectory(prefix="Fishing MVP 中文 ") as temporary:
        missing_root = Path(temporary)
        shutil.copy2(root / "START.bat", missing_root / "START.bat")
        run([cmd, "/d", "/c", str(missing_root / "START.bat")], "\n", 2, "套件不完整")
    # Smoke records are CI diagnostics, not files to ship in the final ZIP.
    shutil.rmtree(root / "run")


if __name__ == "__main__":
    main()
