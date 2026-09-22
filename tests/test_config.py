from pathlib import Path

import pytest

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


@pytest.mark.parametrize("text, field", [
    ("capture_fpz: 10", "capture_fpz"),
    ("action:\n  qte_enable: true", "action.qte_enable"),
    ("action: null", "action"),
    ("detector: []", "detector"),
    ("[]", "config"),
    ("false", "config"),
    ('action:\n  qte_enabled: "false"', "action.qte_enabled"),
    ("action:\n  qte_velocity_samples: 2.5", "action.qte_velocity_samples"),
    ("capture_fps: true", "capture_fps"),
    ('capture_fps: "30"', "capture_fps"),
    ("capture_fps: .nan", "capture_fps"),
    ("action:\n  qte_input_latency_s: .inf", "action.qte_input_latency_s"),
    ("capture_fps: 0", "capture_fps"),
    ("qte_capture_fps: -1", "qte_capture_fps"),
    ("action:\n  min_confidence: 1.1", "action.min_confidence"),
    ("action:\n  tap_hold_ms: -1", "action.tap_hold_ms"),
    ("state_machine:\n  stable_frames: 0", "state_machine.stable_frames"),
    ("scrcpy:\n  frame_timeout_s: 0", "scrcpy.frame_timeout_s"),
    ("detector:\n  min_saturation: 256", "detector.min_saturation"),
    ("detector:\n  purple_hue_high: 180", "detector.purple_hue_high"),
    ("detector:\n  button_min_y_ratio: 0.95", "detector.button_min_y_ratio"),
    ("detector:\n  prompt_min_aspect: 20", "detector.prompt_min_aspect"),
    ('automation:\n  auto_retry_enabled: "false"', "automation.auto_retry_enabled"),
    ("automation:\n  max_retry_attempts: true", "automation.max_retry_attempts"),
    ("automation:\n  max_retry_attempts: -1", "automation.max_retry_attempts"),
    ("automation:\n  max_retry_attempts: 1.5", "automation.max_retry_attempts"),
    ("automation:\n  retry_delay_ms: -1", "automation.retry_delay_ms"),
    ("automation:\n  retry_delay_ms: 1.5", "automation.retry_delay_ms"),
    ("automation:\n  retry_recovery_timeout_s: .inf", "automation.retry_recovery_timeout_s"),
    ("automation:\n  retry_recovery_timeout_s: -1", "automation.retry_recovery_timeout_s"),
])
def test_invalid_config_reports_field(tmp_path, text, field):
    path = tmp_path / "invalid.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        load_config(path)


def test_malformed_yaml_reports_file(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("action: [", encoding="utf-8")
    with pytest.raises(ValueError, match="broken.yaml"):
        load_config(path)


def test_bounds_validate_after_merging_overrides(tmp_path):
    base = tmp_path / "base.yaml"
    override = tmp_path / "user.yaml"
    base.write_text("detector:\n  prompt_max_aspect: 12\n", encoding="utf-8")
    override.write_text("detector:\n  prompt_min_aspect: 15\n  prompt_max_aspect: 20\n", encoding="utf-8")
    assert load_config(override, base_path=base).detector.prompt_min_aspect == 15


def test_packaged_defaults_pass_strict_validation():
    root = Path(__file__).resolve().parents[1]
    assert load_config(root / "config/default.yaml").to_dict() == load_config(
        root / "src/fishing_mvp/defaults/default.yaml"
    ).to_dict()


def test_retry_user_override_preserves_safe_defaults(tmp_path):
    defaults = load_config()
    assert not defaults.automation.auto_retry_enabled
    assert defaults.automation.max_retry_attempts == 3
    assert defaults.automation.retry_delay_ms == 1000
    assert defaults.automation.retry_recovery_timeout_s == 8.0
    path = tmp_path / "user.yaml"
    path.write_text("automation:\n  auto_retry_enabled: true\n  max_retry_attempts: 2\n  retry_delay_ms: 500\n")
    config = load_config(path, base_path=Path(__file__).resolve().parents[1] / "src/fishing_mvp/defaults/default.yaml")
    assert config.automation.auto_retry_enabled
    assert config.automation.max_retry_attempts == 2
    assert config.automation.retry_delay_ms == 500


@pytest.mark.parametrize("mode", ["auto", "reflection", "clip"])
def test_qte_boundary_mode_accepts_supported_modes(tmp_path, mode):
    path = tmp_path / "user.yaml"
    path.write_text(f"action:\n  qte_boundary_mode: {mode}\n", encoding="utf-8")
    assert load_config(path).action.qte_boundary_mode == mode


def test_qte_boundary_mode_rejects_unknown_mode(tmp_path):
    path = tmp_path / "user.yaml"
    path.write_text("action:\n  qte_boundary_mode: invalid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="action.qte_boundary_mode"):
        load_config(path)
