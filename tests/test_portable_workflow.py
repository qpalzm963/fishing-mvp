from __future__ import annotations

from pathlib import Path
import re


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-portable.yml"


def workflow_text() -> str:
    assert WORKFLOW_PATH.is_file()
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_windows_workflow_triggers_package_build_for_all_required_events() -> None:
    workflow = workflow_text()
    trigger_block = workflow.split("\non:", 1)[1].split("\nenv:", 1)[0]

    for trigger in ("push:", "pull_request:", "workflow_dispatch:"):
        assert re.search(rf"(?m)^  {re.escape(trigger)}$", trigger_block)
    assert "runs-on: windows-latest" in workflow
    assert "architecture: x64" in workflow
    assert "uses: actions/checkout@v4" in workflow
    assert "uses: actions/setup-python@v5" in workflow


def test_windows_workflow_installs_and_invokes_the_portable_contract() -> None:
    workflow = workflow_text()

    assert "python -m pip install '.[dev,scrcpy]'" in workflow
    assert "python -m pip install pyinstaller" in workflow
    assert "packaging/build_windows.ps1" in workflow
    assert "& $contract -OutputDirectory $packageRoot" in workflow
    assert 'FISHING_MVP_SCRCPY_VERSION: "4.1"' in workflow
    assert "must pin and record scrcpy 4.1" in workflow
    assert "python -m pytest -q" in workflow
    assert "python -m compileall -q src tests" in workflow


def test_smoke_tests_target_the_assembled_portable_directory() -> None:
    workflow = workflow_text()

    assert "& $exe.FullName --help" in workflow
    for runtime_file in ("scrcpy.exe", "adb.exe", "scrcpy-server", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
        assert runtime_file in workflow
    assert "runtime-manifest.json" in workflow
    assert "scrcpy_version -ne '4.1'" in workflow
    assert "CodecContext.create('h264', 'r')" in workflow
    assert "PyAV files were not found in the frozen portable package" in workflow
    assert "Compress-Archive" in workflow
    assert "uses: actions/upload-artifact@v4" in workflow


def test_release_publish_is_gated_and_permissions_are_minimal() -> None:
    workflow = workflow_text()
    release_job = workflow.split("\n  release:\n", 1)[1]

    assert re.search(r"(?ms)^permissions:\n  contents: read\n", workflow)
    assert re.search(r"(?ms)^    permissions:\n      contents: write\n", release_job)
    assert "needs: windows-portable" in release_job
    assert "if: ${{ startsWith(github.ref, 'refs/tags/v') || github.event_name == 'workflow_dispatch' }}" in release_job
    assert "uses: actions/download-artifact@v4" in release_job
    assert "uses: softprops/action-gh-release@v2" in release_job
    assert "files: release/*.zip" in release_job
    assert "fail_on_unmatched_files: true" in release_job
    assert "action-gh-release" not in workflow.split("\n  release:\n", 1)[0]
    assert "secrets." not in workflow


def test_manual_release_requires_a_versioned_tag_input() -> None:
    workflow = workflow_text()

    assert re.search(r"(?ms)^      release_tag:\n.*?required: true\n.*?type: string", workflow)
    assert "if [[ ! \"$release_tag\" =~ ^v[0-9] ]]" in workflow
