from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.domain import appearance, reminder
from app.domain.models import AppSetting, Attempt, GenerationJob, now_iso
from app.infra import credentials, reminder_scheduler, workspace
from app.infra.db import SessionLocal
from app.infra.ids import new_id
from app.infra.model_client import (
    DeepSeekClient,
    FakeModelClient,
    ModelError,
    ModelRequest,
    TextPayload,
)

logger = logging.getLogger("xunzhi.settings")
router = APIRouter(prefix="/api/settings", tags=["settings"])
APPEARANCE_KEY = "settings.appearance"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _fail(status: int, code: str, message: str, **extra) -> HTTPException:
    error = {"code": code, "message": message, "actions": []}
    error.update(extra)
    return HTTPException(status_code=status, detail={"error": error})


def _desktop_mode() -> bool:
    return settings.desktop_mode or os.getenv("XUNZHI_DESKTOP") == "1"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _appearance(db: Session) -> dict:
    row = db.get(AppSetting, APPEARANCE_KEY)
    if row is None:
        return appearance.normalize(None)
    try:
        raw = json.loads(row.value or "{}")
    except (TypeError, ValueError):
        raw = {}
    return appearance.normalize(raw)


def _save_appearance(db: Session, values: dict) -> dict:
    normalized = appearance.normalize(values)
    row = db.get(AppSetting, APPEARANCE_KEY)
    encoded = json.dumps(normalized, ensure_ascii=False)
    if row is None:
        db.add(AppSetting(key=APPEARANCE_KEY, value=encoded, updated_at=now_iso()))
    else:
        row.value = encoded
        row.updated_at = now_iso()
    db.commit()
    return normalized


def _busy_state(db: Session) -> dict:
    generation = db.execute(
        select(func.count()).select_from(GenerationJob).where(
            GenerationJob.status.in_(("queued", "running"))
        )
    ).scalar_one()
    drafts = db.execute(
        select(func.count()).select_from(Attempt).where(Attempt.status == "draft")
    ).scalar_one()
    return {
        "generation_count": int(generation or 0),
        "draft_count": int(drafts or 0),
        "busy": bool(generation),
    }


class ProfileRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=40)


class CreateUserRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=40)


class SwitchRequest(BaseModel):
    user_id: str


class ModelKeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=400)


class ModelTestRequest(BaseModel):
    api_key: str | None = None


class AppearanceRequest(BaseModel):
    theme: str
    font_size: str
    font_family: str


class ReminderRequest(BaseModel):
    email: str = ""
    time: str = "20:00"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_encryption: str = "starttls"
    smtp_username: str = ""
    password: str | None = None


class ReminderTestRequest(BaseModel):
    confirm_email: str = ""


@router.get("/identity")
def identity(db: Session = Depends(get_db)):
    current = workspace.current_user()
    users = workspace.list_users()
    return {
        "current": workspace.public_user(current),
        "users": [workspace.public_user(item) for item in users],
        "desktop_mode": _desktop_mode(),
        "switch_supported": _desktop_mode(),
        "busy": _busy_state(db),
        "model": settings.model,
        "fake_mode": settings.is_fake,
    }


@router.patch("/profile")
def update_profile(payload: ProfileRequest):
    try:
        user = workspace.rename_user(payload.display_name)
    except ValueError as exc:
        code = str(exc)
        raise _fail(422, code, "显示名需要填写，且不超过 40 个字符。") from exc
    return {"current": workspace.public_user(user)}


@router.post("/users", status_code=201)
def create_user(payload: CreateUserRequest):
    try:
        user = workspace.create_user(payload.display_name)
    except ValueError as exc:
        code = str(exc)
        raise _fail(422, code, "显示名需要填写，且不超过 40 个字符。") from exc
    return {"user": workspace.public_user(user)}


@router.post("/switch", status_code=202)
def switch_user(payload: SwitchRequest, db: Session = Depends(get_db)):
    if not _desktop_mode():
        raise _fail(
            409,
            "desktop_required",
            "切换本机学习者需要使用桌面版；网页服务不会改动运行中的工作区。",
        )
    current = workspace.current_user()
    target = workspace.user_by_id(payload.user_id)
    if target is None:
        raise _fail(404, "user_not_found", "学习者不存在。")
    if target["id"] == current["id"]:
        raise _fail(409, "same_user", "当前已经在这个学习者的工作区。")
    busy = _busy_state(db)
    if busy["busy"]:
        raise _fail(
            409,
            "workspace_busy",
            "当前还有生成任务在进行；请等待任务结束，或取消本次切换。",
            generation_count=busy["generation_count"],
        )
    ok, message = workspace.validate_workspace(target)
    if not ok:
        raise _fail(409, "workspace_unavailable", message)
    request_id = new_id("switch")
    request = {
        "request_id": request_id,
        "target_user_id": target["id"],
        "target_display_name": target["display_name"],
        "previous_user_id": current["id"],
        "target_workspace": str(workspace.workspace_path(target)),
        "created_at": now_iso(),
    }
    _write_json(Path(settings.root_dir) / "switch-request.json", request)
    return {
        "status": "switching",
        "request_id": request_id,
        "target_user_id": target["id"],
        "target_display_name": target["display_name"],
        "draft_count": busy["draft_count"],
    }


@router.get("/switch-result")
def switch_result(request_id: str | None = None):
    payload = _read_json(Path(settings.root_dir) / "switch-result.json")
    if not payload or (request_id and payload.get("request_id") != request_id):
        return {"status": "pending"}
    return payload


@router.get("/model")
def model_status():
    current = workspace.current_user()
    data = credentials.status(
        workspace.current_workspace(),
        settings.api_key,
        inherit_env=bool(current.get("is_default")),
    )
    data.update(
        {
            "model": settings.model,
            "fake_mode": settings.is_fake,
            "can_generate": settings.can_generate,
            "default_source": bool(current.get("is_default")),
        }
    )
    return data


@router.put("/model")
def save_model_key(payload: ModelKeyRequest):
    previous = model_status()
    probe = _probe_candidate(payload.api_key)
    if not probe["ok"]:
        code = probe["code"]
        status = (
            502
            if code in {"network_error", "timeout", "unavailable", "quota_limited", "rate_limited"}
            else 422
        )
        message = probe["message"]
        if previous.get("configured"):
            message = f"{message} 原配置仍可用。"
        raise _fail(
            status,
            code,
            message,
            http_status=probe.get("http_status"),
            previous_configured=bool(previous.get("configured")),
            previous_source=previous.get("source"),
        )
    try:
        credentials.save_key(workspace.current_workspace(), payload.api_key)
    except ValueError as exc:
        raise _fail(422, "api_key_required", "密钥不能为空。") from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("settings model key save failed")
        raise _fail(500, "credential_save_failed", "密钥未保存，原配置仍可用。") from exc
    return model_status()


@router.post("/model/reset")
def reset_model_key():
    credentials.reset_key(workspace.current_workspace())
    return model_status()


def _probe_request() -> ModelRequest:
    return ModelRequest(
        system_prompt="你是连接测试助手。只输出一个 JSON 对象，不要输出 Markdown 或额外文字。",
        task_text=(
            "【连接测试】这是固定的轻量连通测试。"
            '请只返回一个 JSON 对象：{"ok": true}。'
        ),
        text_items=[
            TextPayload(
                ref="T1",
                description='连接测试；请只返回 JSON 对象 {"ok": true}。',
            )
        ],
        question_count=1,
    )


def _probe_failure(exc: ModelError) -> tuple[str, str, int | None]:
    status = getattr(exc, "status_code", None)
    if exc.code == "auth_error" or status == 403:
        return "invalid_key", "密钥无效，请检查后重试。", status
    if exc.code == "no_api_key":
        return "missing_key", "还没有可测试的密钥，请先填写。", status
    if exc.code == "quota_error":
        return "quota_limited", "调用受限：额度不足。", status
    if exc.code == "upstream_rate_limited":
        return "rate_limited", "调用受限：模型服务限流。", status
    if exc.code == "upstream_error" and status == 400:
        return (
            "probe_request_invalid",
            "探测请求格式错误（HTTP 400），不是密钥问题；请报告此问题。",
            status,
        )
    if exc.code == "upstream_error":
        return "unavailable", "模型暂时不可用，请稍后重试。", status
    if exc.code == "network_error":
        return "network_error", "网络错误：无法连接模型服务。", status
    if exc.code == "generation_timeout":
        return "timeout", "连接超时，请检查网络后重试。", status
    return "unavailable", exc.message, status


def _probe_candidate(api_key: str) -> dict:
    mode = "test_double" if settings.allow_fake else settings.model
    client = FakeModelClient() if settings.allow_fake else DeepSeekClient(api_key=api_key)
    try:
        result = client.generate(_probe_request())
    except ModelError as exc:
        code, message, http_status = _probe_failure(exc)
        logger.warning(
            "settings model probe failed code=%s http_status=%s mode=%s",
            code,
            http_status,
            mode,
        )
        return {
            "ok": False,
            "code": code,
            "message": message,
            "http_status": http_status,
            "mode": mode,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "settings model probe failed code=network_error detail=%s mode=%s",
            type(exc).__name__,
            mode,
        )
        return {
            "ok": False,
            "code": "network_error",
            "message": f"网络错误：{type(exc).__name__}",
            "http_status": None,
            "mode": mode,
        }
    try:
        payload = json.loads(str(getattr(result, "content_text", "") or ""))
    except (TypeError, ValueError):
        logger.warning("settings model probe failed code=probe_invalid_response mode=%s", mode)
        return {
            "ok": False,
            "code": "probe_invalid_response",
            "message": "探测响应不是合法 JSON（不是密钥问题）；请报告此问题。",
            "http_status": None,
            "mode": mode,
        }
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        logger.warning("settings model probe failed code=probe_invalid_response mode=%s", mode)
        return {
            "ok": False,
            "code": "probe_invalid_response",
            "message": '探测响应未返回 {"ok": true}（不是密钥问题）；请报告此问题。',
            "http_status": None,
            "mode": mode,
        }
    logger.info("settings model probe ok mode=%s", mode)
    return {
        "ok": True,
        "code": "ok",
        "message": (
            "测试替身连接通过；真实调用需关闭替身模式后由本机验证。"
            if settings.allow_fake
            else "连接成功；请保存后使用这份配置。"
        ),
        "http_status": 200,
        "mode": mode,
    }


@router.post("/model/test")
def test_model_key(payload: ModelTestRequest):
    key = str(payload.api_key or "").strip() or settings.resolved_api_key
    if not key:
        return {
            "ok": False,
            "code": "missing_key",
            "message": "还没有可测试的密钥，请先填写。",
            "mode": "none",
            "http_status": None,
        }
    return _probe_candidate(key)


@router.get("/appearance")
def get_appearance(db: Session = Depends(get_db)):
    return _appearance(db)


@router.put("/appearance")
def save_appearance(payload: AppearanceRequest, db: Session = Depends(get_db)):
    return _save_appearance(db, payload.model_dump())


@router.get("/theme.css")
def theme_css(
    preview_theme: str | None = None,
    preview_font_size: str | None = None,
    preview_font_family: str | None = None,
    db: Session = Depends(get_db),
):
    values = _appearance(db)
    if preview_theme:
        values["theme"] = preview_theme
    if preview_font_size:
        values["font_size"] = preview_font_size
    if preview_font_family:
        values["font_family"] = preview_font_family
    return Response(
        appearance.css(values),
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )

def _reminder_payload(db: Session) -> dict:
    config = reminder.load_config(db)
    status = reminder.load_status(db)
    password = credentials.email_password_status(workspace.current_workspace())
    next_check = reminder.expected_next_check(config, status)
    scheduler = status.get("scheduler") or {"state": "unknown", "message": "尚未注册本机调度。"}
    if not config.get("enabled"):
        next_check = None
    return {
        "config": config,
        "password": password,
        "status": status,
        "timezone": reminder.timezone_label(),
        "next_check_at": next_check,
        "scheduler": scheduler,
    }


def _reminder_validate(config: dict) -> str | None:
    email = str(config.get("email") or "")
    if email and not EMAIL_RE.match(email):
        return "收件邮箱格式不正确。"
    if not reminder.TIME_RE.match(str(config.get("time") or "")):
        return "提醒时间需要 HH:MM 格式。"
    port = int(config.get("smtp_port") or 0)
    if port < 1 or port > 65535:
        return "SMTP 端口需要在 1 到 65535 之间。"
    if config.get("smtp_encryption") not in {"ssl", "starttls", "none"}:
        return "SMTP 加密方式不正确。"
    return None


@router.get("/reminder")
def get_reminder(db: Session = Depends(get_db)):
    return _reminder_payload(db)


@router.put("/reminder")
def save_reminder(payload: ReminderRequest, db: Session = Depends(get_db)):
    current = reminder.load_config(db)
    config = reminder.normalize_config(payload.model_dump(exclude={"password"}))
    config["enabled"] = bool(current.get("enabled"))
    error = _reminder_validate(config)
    if error:
        raise _fail(422, "reminder_config_invalid", error)
    if payload.password:
        try:
            credentials.save_email_password(workspace.current_workspace(), payload.password)
        except ValueError as exc:
            raise _fail(422, "email_password_required", "密码或授权码不能为空。") from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("email credential save failed")
            raise _fail(500, "email_credential_save_failed", "邮件凭据未保存，原配置仍可用。") from exc
    reminder.save_config(db, config)
    return _reminder_payload(db)


@router.post("/reminder/preview")
def preview_reminder(db: Session = Depends(get_db)):
    return reminder.preview(db)


@router.post("/reminder/test")
def test_reminder(payload: ReminderTestRequest, db: Session = Depends(get_db)):
    config = reminder.load_config(db)
    result = reminder.send_test_email(db, config, payload.confirm_email)
    return {"result": result, "reminder": _reminder_payload(db)}


@router.post("/reminder/enable")
def enable_reminder(db: Session = Depends(get_db)):
    config = reminder.load_config(db)
    password = credentials.email_password(workspace.current_workspace())
    complete, reason = reminder.config_complete(config, password)
    if not complete:
        raise _fail(409, "reminder_config_incomplete", reason)
    current = workspace.current_user()
    config["enabled"] = True
    reminder.save_config(db, config)
    registered = reminder_scheduler.register(
        Path(settings.root_dir), current, workspace.current_workspace()
    )
    if not registered.get("ok"):
        config["enabled"] = False
        reminder.save_config(db, config)
        reminder.save_status(
            db,
            {
                "state": "not_enabled",
                "scheduler": registered,
                "last_check_at": datetime.now().replace(microsecond=0).isoformat(),
                "next_check_at": None,
            },
        )
        raise _fail(
            502,
            registered.get("code") or "scheduler_register_failed",
            registered.get("message") or "Windows 任务计划程序注册失败。",
        )
    reminder.save_status(
        db,
        {
            "state": "waiting",
            "scheduler": registered,
            "last_check_at": datetime.now().replace(microsecond=0).isoformat(),
        },
    )
    return _reminder_payload(db)


@router.post("/reminder/pause")
def pause_reminder(db: Session = Depends(get_db)):
    config = reminder.load_config(db)
    config["enabled"] = False
    reminder.save_config(db, config)
    current = workspace.current_user()
    removed = reminder_scheduler.unregister(current["id"])
    reminder.save_status(
        db,
        {
            "state": "paused",
            "scheduler": removed,
            "last_check_at": datetime.now().replace(microsecond=0).isoformat(),
            "next_check_at": None,
        },
    )
    return _reminder_payload(db)
