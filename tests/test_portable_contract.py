from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_build_contract_copies_operator_launchers_and_uses_the_frozen_name():
    script = (ROOT / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")
    spec = (ROOT / "packaging" / "fishing_mvp.spec").read_text(encoding="utf-8")

    for launcher in ("portable\\START.bat", "portable\\STOP.bat", "portable\\使用說明.txt"):
        assert launcher in script
    assert "Copy-Item -LiteralPath $launcherPath" in script
    assert 'name="FishingMVP"' in spec
    assert 'PACKAGE_DEFAULT_FILE = SOURCE_ROOT / "fishing_mvp" / "defaults" / "default.yaml"' in spec


def test_portable_manifest_matches_the_launcher_directory_contract():
    manifest = json.loads((ROOT / "packaging" / "runtime-manifest.json").read_text(encoding="utf-8"))
    start = (ROOT / "portable" / "START.bat").read_text(encoding="utf-8")

    assert manifest["python_entrypoint"] == "runtime/FishingMVP.exe"
    assert manifest["root_launchers"] == ["START.bat", "STOP.bat", "使用說明.txt"]
    assert manifest["runtime_contract"]["required_official_files"] == [
        "runtime/scrcpy/scrcpy.exe",
        "runtime/scrcpy/scrcpy-server",
        "runtime/scrcpy/adb.exe",
        "runtime/scrcpy/AdbWinApi.dll",
        "runtime/scrcpy/AdbWinUsbApi.dll",
    ]
    assert "discover-device --adb-path" in start
    assert "--capture scrcpy" in start
    assert "--full-auto" in start
    assert "--max-rounds" in start


def test_packaged_default_config_is_kept_in_sync_with_the_source_baseline():
    source = (ROOT / "config" / "default.yaml").read_text(encoding="utf-8")
    packaged = (ROOT / "src" / "fishing_mvp" / "defaults" / "default.yaml").read_text(encoding="utf-8")

    # Both files intentionally allow different comments, but all YAML values
    # must match so a frozen build cannot silently use stale detector tuning.
    import yaml

    assert yaml.safe_load(packaged) == yaml.safe_load(source)
