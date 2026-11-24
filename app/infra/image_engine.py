from __future__ import annotations

import hashlib
import io

from PIL import Image

ALLOWED = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def inspect_image(data: bytes) -> dict:
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or ""
            if fmt not in ALLOWED:
                raise ValueError(f"unsupported image format: {fmt}")
            ext = ALLOWED[fmt]
            return {
                "ext": ext,
                "mime": MIME[ext],
                "width": img.width,
                "height": img.height,
            }
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"image unreadable: {exc}") from exc


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_for_model(data: bytes, max_side: int = 1600, max_bytes: int = 2 * 1024 * 1024) -> tuple[bytes, str]:
    with Image.open(io.BytesIO(data)) as img:
        fmt = img.format or "PNG"
        img.load()
        if max(img.width, img.height) > max_side:
            ratio = max_side / max(img.width, img.height)
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))))
        buffer = io.BytesIO()
        if fmt == "WEBP":
            img.save(buffer, format="WEBP", quality=88)
            mime = "image/webp"
        elif fmt == "JPEG":
            img.convert("RGB").save(buffer, format="JPEG", quality=88)
            mime = "image/jpeg"
        else:
            img.save(buffer, format="PNG", optimize=True)
            mime = "image/png"
        payload = buffer.getvalue()
        if len(payload) > max_bytes and mime != "image/png":
            buffer = io.BytesIO()
            img.convert("RGB").save(buffer, format="JPEG", quality=80)
            payload = buffer.getvalue()
            mime = "image/jpeg"
        if len(payload) > max_bytes:
            for quality in (80, 65, 50):
                buffer = io.BytesIO()
                img.convert("RGB").save(buffer, format="JPEG", quality=quality)
                payload = buffer.getvalue()
                if len(payload) <= max_bytes:
                    mime = "image/jpeg"
                    break
        return payload, mime


def make_thumb(data: bytes, max_side: int = 480) -> bytes:
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=82)
        return buffer.getvalue()

def crop_region(data: bytes, box) -> bytes:
    """按相对坐标 [left, top, right, bottom]（0–1）裁剪区域，坐标非法时返回原图。"""
    try:
        values = [float(value) for value in box]
    except (TypeError, ValueError):
        return data
    if (
        len(values) != 4
        or not all(0.0 <= value <= 1.0 for value in values)
        or values[2] <= values[0]
        or values[3] <= values[1]
    ):
        return data
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        width, height = img.size
        left = max(0, int(values[0] * width))
        top = max(0, int(values[1] * height))
        right = min(width, max(left + 1, int(values[2] * width)))
        bottom = min(height, max(top + 1, int(values[3] * height)))
        crop = img.crop((left, top, right, bottom))
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()