from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.infra.ids import new_id

USERS_FILE = "users.json"
DEFAULT_USER_ID = "user_default"
DEFAULT_DISPLAY_NAME = "本机用户"
MAX_DISPLAY_NAME = 40


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def data_root() -> Path:
    return Path(settings.root_dir)


def users_path(root: Path | None = None) -> Path:
    return (root or data_root()) / USERS_FILE


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(handle.name, path)
    finally:
        try:
            Path(handle.name).unlink()
        except OSError:
            pass


def _default_record() -> dict:
    return {
        "id": DEFAULT_USER_ID,
        "display_name": DEFAULT_DISPLAY_NAME,
        "workspace": ".",
        "is_default": True,
        "created_at": _now(),
    }


def _normalize(record: dict) -> dict:
    return {
        "id": str(record.get("id") or ""),
        "display_name": str(record.get("display_name") or "").strip()
        or DEFAULT_DISPLAY_NAME,
        "workspace": str(record.get("workspace") or "."),
        "is_default": bool(record.get("is_default")),
        "created_at": str(record.get("created_at") or _now()),
    }


def load_registry(root: Path | None = None) -> dict:
    path = users_path(root)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            users = [_normalize(item) for item in raw.get("users") or [] if item.get("id")]
            current = str(raw.get("current_user_id") or "")
        except (OSError, ValueError, TypeError):
            users, current = [], ""
    else:
        users, current = [], ""
    if not any(item["is_default"] for item in users):
        users.insert(0, _default_record())
    if not current or not any(item["id"] == current for item in users):
        current = next(item["id"] for item in users if item["is_default"])
    return {"version": 1, "current_user_id": current, "users": users}


def ensure_registry(root: Path | None = None) -> dict:
    registry = load_registry(root)
    path = users_path(root)
    if not path.exists():
        _atomic_write(path, registry)
    return registry


def _save(registry: dict, root: Path | None = None) -> None:
    _atomic_write(users_path(root), registry)


def list_users(root: Path | None = None) -> list[dict]:
    return ensure_registry(root)["users"]


def workspace_path(user: dict, root: Path | None = None) -> Path:
    base = (root or data_root()).resolve()
    raw = Path(str(user.get("workspace") or "."))
    if raw.is_absolute():
        return raw
    return (base / raw).resolve()


def user_by_id(user_id: str, root: Path | None = None) -> dict | None:
    for item in list_users(root):
        if item["id"] == user_id:
            return item
    return None


def current_user(root: Path | None = None) -> dict:
    registry = ensure_registry(root)
    env_id = (settings.user_id or "").strip()
    if env_id:
        found = next((item for item in registry["users"] if item["id"] == env_id), None)
        if found is not None:
            return found
    if not settings.desktop_mode and not env_id:
        return next(item for item in registry["users"] if item["is_default"])
    current = next(
        (item for item in registry["users"] if item["id"] == registry["current_user_id"]),
        None,
    )
    if current is not None:
        return current
    return next(item for item in registry["users"] if item["is_default"])


def is_default_user(user: dict | None = None) -> bool:
    user = user or current_user()
    return bool(user.get("is_default"))


def current_workspace(root: Path | None = None) -> Path:
    return workspace_path(current_user(root), root)


def create_user(display_name: str, root: Path | None = None) -> dict:
    name = str(display_name or "").strip()
    if not name:
        raise ValueError("display_name_required")
    if len(name) > MAX_DISPLAY_NAME:
        raise ValueError("display_name_too_long")
    registry = ensure_registry(root)
    user_id = new_id("user")
    workspace = f"users/{user_id}"
    base = (root or data_root()).resolve()
    target = (base / workspace).resolve()
    target.mkdir(parents=True, exist_ok=True)
    record = {
        "id": user_id,
        "display_name": name,
        "workspace": workspace,
        "is_default": False,
        "created_at": _now(),
    }
    registry["users"].append(record)
    _save(registry, root)
    return record


def rename_user(display_name: str, root: Path | None = None) -> dict:
    current = current_user(root)
    return rename_user_by_id(current["id"], display_name, root)


def rename_user_by_id(
    user_id: str, display_name: str, root: Path | None = None
) -> dict:
    name = str(display_name or "").strip()
    if not name:
        raise ValueError("display_name_required")
    if len(name) > MAX_DISPLAY_NAME:
        raise ValueError("display_name_too_long")
    registry = ensure_registry(root)
    for item in registry["users"]:
        if item["id"] == user_id:
            item["display_name"] = name
            _save(registry, root)
            return item
    raise ValueError("user_not_found")


def set_current(user_id: str, root: Path | None = None) -> dict:
    registry = ensure_registry(root)
    target = next((item for item in registry["users"] if item["id"] == user_id), None)
    if target is None:
        raise ValueError("user_not_found")
    registry["current_user_id"] = user_id
    _save(registry, root)
    return target


def validate_workspace(user: dict, root: Path | None = None) -> tuple[bool, str]:
    try:
        path = workspace_path(user, root)
    except OSError:
        return False, "目标工作区路径不可用。"
    if path.exists() and not path.is_dir():
        return False, "目标工作区不是目录。"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"目标工作区不可写：{exc}"
    return True, ""


def public_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "display_name": user["display_name"],
        "is_default": bool(user["is_default"]),
        "workspace": str(user["workspace"]),
        "created_at": user.get("created_at"),
    }