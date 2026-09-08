from pathlib import Path

from fishing_mvp.runtime import PortablePaths


def test_portable_paths_match_issue_three_directory_contract(tmp_path: Path):
    paths = PortablePaths.from_root(tmp_path)

    assert paths.runtime_dir == tmp_path / "runtime"
    assert paths.scrcpy_dir == tmp_path / "runtime" / "scrcpy"
    assert paths.adb.name == "adb.exe"
    assert paths.scrcpy.name == "scrcpy.exe"
    assert paths.scrcpy_server.name == "scrcpy-server"
    assert paths.user_config == tmp_path / "config" / "user.yaml"


def test_frozen_executable_inside_runtime_resolves_package_root(tmp_path: Path):
    executable_dir = tmp_path / "runtime"
    paths = PortablePaths.from_root(tmp_path, executable_dir=executable_dir)

    assert paths.root == tmp_path.resolve()
    assert paths.executable_dir == executable_dir.resolve()


def test_missing_bundled_files_is_explicit_and_non_destructive(tmp_path: Path):
    paths = PortablePaths.from_root(tmp_path)
    (paths.scrcpy_dir).mkdir(parents=True)
    paths.adb.touch()

    assert paths.missing_bundled_files() == [paths.scrcpy, paths.scrcpy_server]
