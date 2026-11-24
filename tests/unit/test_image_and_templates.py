from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from app.config import PROJECT_ROOT
from app.infra import image_engine


def make_image(fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2400, 1200), (30, 100, 70)).save(buffer, format=fmt)
    return buffer.getvalue()


def test_real_formats_are_detected():
    assert image_engine.inspect_image(make_image("PNG"))["ext"] == "png"
    assert image_engine.inspect_image(make_image("JPEG"))["ext"] == "jpg"
    assert image_engine.inspect_image(make_image("WEBP"))["ext"] == "webp"


def test_model_image_is_resized_and_bounded():
    payload, mime = image_engine.normalize_for_model(make_image("PNG"))
    with Image.open(io.BytesIO(payload)) as img:
        assert max(img.width, img.height) <= 1600
    assert len(payload) <= 2 * 1024 * 1024
    assert mime in {"image/png", "image/jpeg", "image/webp"}


def test_thumbnail_is_created():
    thumb = image_engine.make_thumb(make_image("PNG"))
    with Image.open(io.BytesIO(thumb)) as img:
        assert max(img.width, img.height) <= 480


def test_templates_do_not_expose_key_or_model_controls():
    for name in ("index.html", "pack.html", "records.html", "offline.html"):
        text = (PROJECT_ROOT / "app" / "templates" / name).read_text(encoding="utf-8")
        assert "DEEPSEEK_API_KEY" not in text
        assert 'name="model"' not in text
        assert "temperature" not in text


def test_offline_and_test_double_labels_exist():
    index_text = (PROJECT_ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    chrome_text = (PROJECT_ROOT / "app" / "templates" / "_chrome.html").read_text(encoding="utf-8")
    offline_text = (PROJECT_ROOT / "app" / "templates" / "offline.html").read_text(encoding="utf-8")
    pack_text = (PROJECT_ROOT / "app" / "templates" / "pack.html").read_text(encoding="utf-8")
    records_text = (PROJECT_ROOT / "app" / "templates" / "records.html").read_text(encoding="utf-8")
    assert "设置" in chrome_text
    assert "离线示例" in offline_text
    assert "离线示例" in records_text
    assert "test-banner" in pack_text