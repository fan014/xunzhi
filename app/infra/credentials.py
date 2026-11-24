from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SECRETS_FILE = "secrets.json"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi(data: bytes, protect: bool) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        source, _source_buffer = _blob(data)
        output = _DataBlob()
        if protect:
            ok = crypt32.CryptProtectData(
                ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
            )
        else:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
            )
        if not ok:
            return None
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(output.pbData)
    except Exception:
        return None


def _encode(data: bytes) -> str:
    protected = _dpapi(data, True)
    if protected is not None:
        return "dpapi:" + base64.b64encode(protected).decode("ascii")
    return "b64:" + base64.b64encode(data).decode("ascii")


def _decode(value: str) -> bytes | None:
    if not value:
        return None
    if value.startswith("dpapi:"):
        raw = base64.b64decode(value.split(":", 1)[1])
        return _dpapi(raw, False)
    if value.startswith("b64:"):
        return base64.b64decode(value.split(":", 1)[1])
    return None


def secrets_path(workspace: Path) -> Path:
    return Path(workspace) / SECRETS_FILE


def read_record(workspace: Path) -> dict | None:
    path = secrets_path(workspace)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("mode") != "custom":
        return None
    return raw


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(handle.name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            Path(handle.name).unlink()
        except OSError:
            pass


def save_key(workspace: Path, api_key: str) -> dict:
    key = str(api_key or "").strip()
    if not key:
        raise ValueError("api_key_required")
    payload = {
        "version": 1,
        "mode": "custom",
        "key": _encode(key.encode("utf-8")),
        "updated_at": _now(),
    }
    _atomic_write(secrets_path(workspace), payload)
    return status(workspace, "", inherit_env=False)


def reset_key(workspace: Path) -> dict:
    path = secrets_path(workspace)
    try:
        path.unlink()
    except OSError:
        pass
    return status(workspace, "", inherit_env=True)


def custom_key(workspace: Path) -> str:
    record = read_record(workspace)
    if record is None:
        return ""
    try:
        raw = _decode(str(record.get("key") or ""))
    except (ValueError, binascii.Error):
        return ""
    if raw is None:
        return ""
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def effective_api_key(workspace: Path, env_key: str, *, inherit_env: bool) -> str:
    key = custom_key(workspace)
    if key:
        return key
    if inherit_env:
        return (env_key or "").strip()
    return ""


def masked(key: str) -> str:
    value = str(key or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"••••{value[-4:]}"


def status(workspace: Path, env_key: str, *, inherit_env: bool) -> dict:
    key = custom_key(workspace)
    if key:
        return {
            "configured": True,
            "source": "user",
            "source_label": "本用户已保存的配置",
            "masked": masked(key),
            "stored": "protected",
        }
    if inherit_env and (env_key or "").strip():
        return {
            "configured": True,
            "source": "env",
            "source_label": "使用已有配置（.env）",
            "masked": masked(env_key),
            "stored": "env",
        }
    return {
        "configured": False,
        "source": "none",
        "source_label": "尚未配置",
        "masked": "",
        "stored": "none",
    }

EMAIL_SECRETS_FILE = "email_secrets.json"


def email_secrets_path(workspace: Path) -> Path:
    return Path(workspace) / EMAIL_SECRETS_FILE


def save_email_password(workspace: Path, password: str) -> dict:
    value = str(password or "")
    if not value:
        raise ValueError("email_password_required")
    payload = {
        "version": 1,
        "mode": "custom",
        "password": _encode(value.encode("utf-8")),
        "updated_at": _now(),
    }
    _atomic_write(email_secrets_path(workspace), payload)
    return email_password_status(workspace)


def email_password(workspace: Path) -> str:
    path = email_secrets_path(workspace)
    if not path.exists():
        return ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict) or raw.get("mode") != "custom":
        return ""
    try:
        decoded = _decode(str(raw.get("password") or ""))
    except (ValueError, binascii.Error):
        return ""
    if decoded is None:
        return ""
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def reset_email_password(workspace: Path) -> dict:
    try:
        email_secrets_path(workspace).unlink()
    except OSError:
        pass
    return email_password_status(workspace)


def email_password_status(workspace: Path) -> dict:
    value = email_password(workspace)
    return {
        "configured": bool(value),
        "masked": masked(value) if value else "",
        "stored": "protected" if value else "none",
    }
