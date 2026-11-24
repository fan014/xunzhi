from __future__ import annotations

from pathlib import Path

from app.infra import credentials


def test_key_is_not_stored_in_plaintext_and_status_is_masked(tmp_path: Path):
    workspace = tmp_path / "ws"
    key = "sk-test-secret-1234567890"
    status = credentials.save_key(workspace, key)
    assert status["configured"] is True
    assert status["source"] == "user"
    assert status["masked"].endswith("7890")
    assert key not in status["masked"]
    raw = (workspace / "secrets.json").read_text(encoding="utf-8")
    assert key not in raw
    assert credentials.custom_key(workspace) == key


def test_reset_and_env_fallback_are_profile_specific(tmp_path: Path):
    default_ws = tmp_path / "default"
    new_ws = tmp_path / "users" / "b"
    env_key = "sk-env-abcdefghijkl"
    assert credentials.effective_api_key(default_ws, env_key, inherit_env=True) == env_key
    assert credentials.effective_api_key(new_ws, env_key, inherit_env=False) == ""
    assert credentials.status(default_ws, env_key, inherit_env=True)["source"] == "env"
    assert credentials.status(new_ws, env_key, inherit_env=False)["source"] == "none"

    credentials.save_key(default_ws, "sk-custom-9999")
    assert credentials.effective_api_key(default_ws, env_key, inherit_env=True) == "sk-custom-9999"
    credentials.reset_key(default_ws)
    assert credentials.effective_api_key(default_ws, env_key, inherit_env=True) == env_key
    assert not (default_ws / "secrets.json").exists()