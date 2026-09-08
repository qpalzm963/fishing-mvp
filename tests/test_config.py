from pathlib import Path

from fishing_mvp.config import load_config


def test_load_config_applies_user_override_on_top_of_packaged_default(tmp_path: Path):
    default_path = tmp_path / "default.yaml"
    user_path = tmp_path / "user.yaml"
    default_path.write_text(
        "capture_fps: 10\nqte_capture_fps: 30\naction:\n  qte_input_latency_s: 0.18\n",
        encoding="utf-8",
    )
    user_path.write_text(
        "qte_capture_fps: 24\naction:\n  qte_input_latency_s: 0.12\n",
        encoding="utf-8",
    )

    config = load_config(user_path, base_path=default_path)

    assert config.capture_fps == 10.0
    assert config.qte_capture_fps == 24.0
    assert config.action.qte_input_latency_s == 0.12


def test_load_config_without_files_keeps_dataclass_defaults():
    config = load_config()

    assert config.capture_fps == 10.0
    assert config.qte_capture_fps == 30.0
