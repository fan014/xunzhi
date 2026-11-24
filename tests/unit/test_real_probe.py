from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from app.domain.schemas import ModelOutput, RefMap, RefTarget
from app.domain.validation import ValidationError, validate_output
from app.config import settings
from app.infra.model_client import DeepSeekClient, ImagePayload, ModelRequest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "model_output" / "generate_3q_ok.json"


def test_test_environment_shields_local_key_and_forces_fake():
    assert settings.api_key == ""
    assert settings.allow_fake is True
    assert settings.can_generate is True


def fixture_output() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def ref_map() -> RefMap:
    return RefMap(
        {
            "F1": RefTarget(
                evidence_id="ev_real_probe",
                source_id="src_real_probe",
                kind="image_region",
                page_no=None,
                confidence="high",
                locator_json="{}",
                quote_text="自制拓扑图",
            )
        }
    )


def test_production_client_sends_images_and_distinct_progress():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": fixture_output()}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    transport = httpx.MockTransport(handler)
    client = DeepSeekClient(api_key="test-key", model="deepseek-flash", transport=transport)
    image = ImagePayload(
        ref="F1",
        description="【图 F1】自制拓扑图",
        data=b"\x89PNG\r\n\x1a\nfake-image-bytes",
        mime="image/png",
    )
    progress_a = "我学完了交换机转发，还没学路由协议。"
    progress_b = "我刚开始学习，只想认清图中的设备和作用。"

    first = client.generate(
        ModelRequest(system_prompt="sys", task_text=progress_a, images=[image], question_count=3)
    )
    second = client.generate(
        ModelRequest(system_prompt="sys", task_text=progress_b, images=[image], question_count=3)
    )
    assert first.content_text and second.content_text
    assert len(seen) == 2

    image_urls = []
    for payload in seen:
        blocks = payload["messages"][1]["content"]
        images = [block for block in blocks if block["type"] == "image_url"]
        assert images
        url = images[0]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        image_urls.append(url)
        texts = "\n".join(block["text"] for block in blocks if block["type"] == "text")
        assert "[T" in texts or "【图" in texts
    assert image_urls[0] == image_urls[1]
    assert progress_a in json.dumps(seen[0], ensure_ascii=False)
    assert progress_b in json.dumps(seen[1], ensure_ascii=False)
    assert progress_a not in json.dumps(seen[1], ensure_ascii=False)


def test_invalid_citation_is_rejected():
    data = json.loads(fixture_output())
    data["questions"][0]["citations"] = [{"ref": "F99", "claim": "不存在的引用"}]
    output = ModelOutput.model_validate(data)
    with pytest.raises(ValidationError) as info:
        validate_output(output, 3, ref_map(), True)
    assert info.value.code == "citation_not_found"


def load_probe_module():
    spec = importlib.util.spec_from_file_location("smoke_real_probe", ROOT / "scripts" / "smoke_real.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_isolated_env_ignores_user_data_dir(tmp_path, monkeypatch):
    sentinel = tmp_path / "user-data"
    sentinel.mkdir()
    marker = sentinel / "personal.sqlite3"
    marker.write_text("keep", encoding="utf-8")

    keys = ["XUNZHI_DATA_DIR", "XUNZHI_ALLOW_FAKE", "XUNZHI_FAKE_MODE", "XUNZHI_INSTANCE_ID"]
    previous = {key: os.environ.get(key) for key in keys}
    monkeypatch.setenv("XUNZHI_DATA_DIR", str(sentinel))
    module = load_probe_module()
    root = module.prepare_isolated_env()
    try:
        assert Path(os.environ["XUNZHI_DATA_DIR"]).resolve() == root
        assert root != sentinel.resolve()
        assert root.exists()
        assert marker.exists()
        assert os.environ["XUNZHI_ALLOW_FAKE"] == "0"
    finally:
        assert module.cleanup_isolated_env(root)
        assert not root.exists()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_cleanup_refuses_existing_non_temp_path(tmp_path):
    keep = tmp_path / "existing-data"
    keep.mkdir()
    (keep / "keep.txt").write_text("do not delete", encoding="utf-8")
    module = load_probe_module()
    assert module.cleanup_isolated_env(keep) is False
    assert (keep / "keep.txt").read_text(encoding="utf-8") == "do not delete"

def test_main_without_key_never_prepares_or_requests(monkeypatch, capsys):
    module = load_probe_module()
    called: list[bool] = []
    monkeypatch.setattr(module, "load_dotenv_only", lambda: None)
    monkeypatch.setattr(module, "prepare_isolated_env", lambda: called.append(True))
    monkeypatch.setattr(sys, "argv", ["smoke_real.py"])
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert module.main() == 2
    assert called == []
    captured = capsys.readouterr().out
    assert "未配置" in captured
    assert "sk-" not in captured