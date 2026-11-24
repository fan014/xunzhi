from __future__ import annotations

import json
from pathlib import Path

from app.config import settings
from app.api import settings as settings_api


def _reset_settings(client):
    client.put(
        "/api/settings/appearance",
        json={"theme": "system", "font_size": "standard", "font_family": "system"},
    )
    client.post("/api/settings/model/reset")


def test_settings_page_identity_and_appearance(client):
    _reset_settings(client)
    page = client.get("/settings")
    assert page.status_code == 200
    assert "个人" in page.text and "模型" in page.text and "外观" in page.text
    identity = client.get("/api/settings/identity").json()
    assert identity["current"]["is_default"] is True
    assert identity["users"]
    assert identity["switch_supported"] is False

    saved = client.put(
        "/api/settings/appearance",
        json={"theme": "dark", "font_size": "large", "font_family": "reading"},
    ).json()
    assert saved == {"theme": "dark", "font_size": "large", "font_family": "reading"}
    assert client.get("/api/settings/appearance").json() == saved
    css = client.get("/api/settings/theme.css").text
    assert "#11161d" in css
    assert "1.14" in css
    assert "Noto Serif SC" in css
    assert "prefers-color-scheme" not in css

    preview = client.get(
        "/api/settings/theme.css?preview_theme=system&preview_font_size=small&preview_font_family=system"
    ).text
    assert "prefers-color-scheme" in preview
    assert "0.94" in preview
    assert client.get("/api/settings/appearance").json() == saved

    _reset_settings(client)


def test_model_key_is_not_echoed_and_reset_restores_source(client):
    _reset_settings(client)
    key = "sk-test-hidden-1234567890"
    saved = client.put("/api/settings/model", json={"api_key": key}).json()
    assert saved["configured"] is True
    assert saved["source"] == "user"
    assert saved["masked"].endswith("7890")
    assert key not in json.dumps(saved, ensure_ascii=False)
    secret_path = Path(settings.root_dir) / "secrets.json"
    assert secret_path.exists()
    assert key not in secret_path.read_text(encoding="utf-8")
    assert key not in json.dumps(client.get("/api/settings/model").json(), ensure_ascii=False)

    reset = client.post("/api/settings/model/reset").json()
    assert reset["source"] in {"none", "env"}
    assert not secret_path.exists()


def test_switch_requires_desktop_and_writes_controlled_request(client, monkeypatch):
    _reset_settings(client)
    created = client.post(
        "/api/settings/users", json={"display_name": "切换测试学习者"}
    ).json()["user"]
    monkeypatch.delenv("XUNZHI_DESKTOP", raising=False)
    blocked = client.post("/api/settings/switch", json={"user_id": created["id"]})
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "desktop_required"

    monkeypatch.setenv("XUNZHI_DESKTOP", "1")
    monkeypatch.setattr(
        settings_api,
        "_busy_state",
        lambda db: {"generation_count": 0, "draft_count": 0, "busy": False},
    )
    request_path = Path(settings.root_dir) / "switch-request.json"
    try:
        accepted = client.post("/api/settings/switch", json={"user_id": created["id"]})
        assert accepted.status_code == 202, accepted.text
        body = accepted.json()
        assert body["status"] == "switching"
        assert request_path.exists()
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        assert payload["target_user_id"] == created["id"]
        assert payload["request_id"] == body["request_id"]
        assert client.get(
            f"/api/settings/switch-result?request_id={body['request_id']}"
        ).json()["status"] == "pending"
    finally:
        try:
            request_path.unlink()
        except OSError:
            pass
        monkeypatch.delenv("XUNZHI_DESKTOP", raising=False)


def test_profile_rename_keeps_user_id(client):
    identity = client.get("/api/settings/identity").json()
    original = identity["current"]
    renamed = client.patch(
        "/api/settings/profile", json={"display_name": "本机用户"}
    ).json()["current"]
    assert renamed["id"] == original["id"]
    assert renamed["display_name"] == "本机用户"

def test_probe_request_requires_json_object_and_fake_returns_ok(client):
    from app.api import settings as settings_api

    request = settings_api._probe_request()
    assert "json" in request.task_text.lower()
    assert '"ok": true' in request.task_text
    assert "json" in request.system_prompt.lower()
    probe = settings_api._probe_candidate("sk-test-probe")
    assert probe["ok"] is True
    assert probe["code"] == "ok"
    assert probe["mode"] == "test_double"


def test_probe_format_error_is_not_reported_as_invalid_key(monkeypatch):
    from types import SimpleNamespace

    from app.api import settings as settings_api
    from app.infra.model_client import ModelError

    class BadProbe:
        def generate(self, request):
            return SimpleNamespace(content_text="not-a-json-object")

    monkeypatch.setattr(settings_api, "FakeModelClient", BadProbe)
    probe = settings_api._probe_candidate("sk-test-probe")
    assert probe["ok"] is False
    assert probe["code"] == "probe_invalid_response"
    assert "不是密钥问题" in probe["message"]

    code, message, status = settings_api._probe_failure(
        ModelError("upstream_error", "bad request", 400)
    )
    assert code == "probe_request_invalid"
    assert status == 400
    assert "不是密钥问题" in message


def test_save_rejects_failed_candidate_and_keeps_original_key(client, monkeypatch):
    from pathlib import Path

    from app.api import settings as settings_api
    from app.config import settings

    _reset_settings(client)
    old_key = "sk-old-valid-1234567890"
    first = client.put("/api/settings/model", json={"api_key": old_key})
    assert first.status_code == 200, first.text
    secret_path = Path(settings.root_dir) / "secrets.json"
    before = secret_path.read_text(encoding="utf-8")
    before_status = client.get("/api/settings/model").json()
    assert before_status["source"] == "user"

    monkeypatch.setattr(
        settings_api,
        "_probe_candidate",
        lambda key: {
            "ok": False,
            "code": "invalid_key",
            "message": "密钥无效，请检查后重试。",
            "http_status": 401,
            "mode": "deepseek-flash",
        },
    )
    rejected = client.put(
        "/api/settings/model", json={"api_key": "sk-invalid-0000000000"}
    )
    assert rejected.status_code == 422, rejected.text
    error = rejected.json()["error"]
    assert error["code"] == "invalid_key"
    assert error["previous_configured"] is True
    assert "原配置仍可用" in error["message"]
    assert secret_path.read_text(encoding="utf-8") == before
    after_status = client.get("/api/settings/model").json()
    assert after_status["source"] == "user"
    assert after_status["masked"] == before_status["masked"]

    monkeypatch.setattr(
        settings_api,
        "_probe_candidate",
        lambda key: {
            "ok": True,
            "code": "ok",
            "message": "连接成功",
            "http_status": 200,
            "mode": "deepseek-flash",
        },
    )
    accepted = client.put(
        "/api/settings/model", json={"api_key": "sk-new-valid-9999999999"}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["source"] == "user"
    _reset_settings(client)


def test_probe_failure_codes_distinguish_timeout_rate_and_format():
    from app.api import settings as settings_api
    from app.infra.model_client import ModelError

    assert settings_api._probe_failure(ModelError("generation_timeout", "", None))[0] == "timeout"
    assert settings_api._probe_failure(ModelError("network_error", "", None))[0] == "network_error"
    assert (
        settings_api._probe_failure(ModelError("upstream_rate_limited", "", 429))[0]
        == "rate_limited"
    )
    assert settings_api._probe_failure(ModelError("quota_error", "", 402))[0] == "quota_limited"
    assert (
        settings_api._probe_failure(ModelError("upstream_error", "", 400))[0]
        == "probe_request_invalid"
    )
