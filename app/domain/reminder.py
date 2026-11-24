from __future__ import annotations

import html
import json
import logging
import re
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain import schedule as schedule_service
from app.domain.models import AppSetting, LearningPlan, now_iso
from app.infra import credentials, mailer, workspace

logger = logging.getLogger("xunzhi.reminder")

CONFIG_KEY = "settings.reminder"
STATUS_KEY = "settings.reminder.status"
DELIVERY_PREFIX = "reminder.delivery."
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ENTRY_HINT = "打开循知 → 学习日历"
TYPE_LABELS = {"reading": "阅读", "review": "回顾", "practice": "练习"}

DEFAULT_CONFIG = {
    "enabled": False,
    "email": "",
    "time": "20:00",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_encryption": "starttls",
    "smtp_username": "",
}
DEFAULT_STATUS = {
    "state": "not_enabled",
    "last_check_at": None,
    "last_result_at": None,
    "task_count": 0,
    "minutes": 0,
    "error": None,
    "next_check_at": None,
    "last_test": None,
}


def local_now() -> datetime:
    return datetime.now().replace(microsecond=0)


def timezone_label() -> str:
    now = local_now().astimezone()
    return f"{now.tzname() or '本机时区'}（{now.strftime('%z')}）"


def load_config(db: Session) -> dict:
    row = db.get(AppSetting, CONFIG_KEY)
    values = dict(DEFAULT_CONFIG)
    if row is not None:
        try:
            raw = json.loads(row.value or "{}")
        except (TypeError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            values.update({key: raw[key] for key in DEFAULT_CONFIG if key in raw})
    values["enabled"] = bool(values.get("enabled"))
    try:
        values["smtp_port"] = int(values.get("smtp_port") or 587)
    except (TypeError, ValueError):
        values["smtp_port"] = 587
    if values.get("smtp_encryption") not in {"ssl", "starttls", "none"}:
        values["smtp_encryption"] = "starttls"
    if not TIME_RE.match(str(values.get("time") or "")):
        values["time"] = "20:00"
    return values


def normalize_config(values: dict) -> dict:
    config = dict(DEFAULT_CONFIG)
    config.update({key: values.get(key, config.get(key)) for key in DEFAULT_CONFIG})
    config["enabled"] = bool(config.get("enabled"))
    config["email"] = str(config.get("email") or "").strip()
    config["time"] = str(config.get("time") or "").strip()
    config["smtp_host"] = str(config.get("smtp_host") or "").strip()
    config["smtp_username"] = str(config.get("smtp_username") or "").strip()
    config["smtp_encryption"] = str(config.get("smtp_encryption") or "starttls").strip()
    try:
        config["smtp_port"] = int(config.get("smtp_port") or 587)
    except (TypeError, ValueError):
        config["smtp_port"] = 587
    if config["smtp_encryption"] not in {"ssl", "starttls", "none"}:
        config["smtp_encryption"] = "starttls"
    return config


def save_config(db: Session, values: dict) -> dict:
    config = normalize_config(values)
    _save_setting(db, CONFIG_KEY, config)
    return config


def load_status(db: Session) -> dict:
    row = db.get(AppSetting, STATUS_KEY)
    status = dict(DEFAULT_STATUS)
    if row is not None:
        try:
            raw = json.loads(row.value or "{}")
        except (TypeError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            status.update(raw)
    return status


def save_status(db: Session, values: dict) -> dict:
    status = load_status(db)
    status.update(values)
    status["updated_at"] = now_iso()
    _save_setting(db, STATUS_KEY, status)
    return status


def _save_setting(db: Session, key: str, payload: dict) -> None:
    row = db.get(AppSetting, key)
    value = json.dumps(payload, ensure_ascii=False)
    if row is None:
        db.add(AppSetting(key=key, value=value, updated_at=now_iso()))
    else:
        row.value = value
        row.updated_at = now_iso()
    db.commit()


def _parse_time(value: str) -> time | None:
    match = TIME_RE.match(str(value or ""))
    if not match:
        return None
    return time(int(match.group(1)), int(match.group(2)))


def _delivery_key(target: date) -> str:
    current = workspace.current_user()
    return f"{DELIVERY_PREFIX}{current['id']}.{target.isoformat()}.daily"


def _load_delivery(db: Session, target: date) -> dict | None:
    row = db.get(AppSetting, _delivery_key(target))
    if row is None:
        return None
    try:
        value = json.loads(row.value or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _write_delivery(db: Session, target: date, value: dict) -> None:
    key = _delivery_key(target)
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=json.dumps(value, ensure_ascii=False), updated_at=now_iso()))
    else:
        row.value = json.dumps(value, ensure_ascii=False)
        row.updated_at = now_iso()
    db.commit()


def _delivery_sent(db: Session, target: date) -> bool:
    delivery = _load_delivery(db, target)
    return bool(delivery and delivery.get("state") == "sent")


def daily_digest(db: Session, target: date) -> dict:
    plans = list(
        db.execute(
            select(LearningPlan)
            .where(LearningPlan.current_version_id.is_not(None))
            .order_by(LearningPlan.created_at, LearningPlan.id)
        ).scalars().all()
    )
    groups = []
    total = 0
    total_minutes = 0
    for plan in plans:
        tasks = [
            task
            for task in schedule_service.tasks_for_version(db, plan.current_version_id)
            if task.scheduled_date == target.isoformat() and task.status != "completed"
        ]
        if not tasks:
            continue
        items = []
        for task in tasks:
            minutes = int(task.estimated_minutes or 0)
            items.append(
                {
                    "id": task.id,
                    "title": task.task_title or task.module_title or "学习任务",
                    "module_title": task.module_title or "",
                    "task_type": task.task_type,
                    "type_label": TYPE_LABELS.get(task.task_type, "任务"),
                    "minutes": minutes,
                }
            )
            total += 1
            total_minutes += minutes
        groups.append(
            {
                "plan_id": plan.id,
                "title": plan.title or "学习计划",
                "tasks": items,
                "task_count": len(items),
                "minutes": sum(item["minutes"] for item in items),
            }
        )
    summary = _summary(groups, total_minutes)
    return {
        "date": target.isoformat(),
        "groups": groups,
        "task_count": total,
        "minutes": total_minutes,
        "summary": summary,
    }


def _summary(groups: list[dict], minutes: int) -> str:
    if not groups:
        return "今天没有待完成的学习任务。"
    first = groups[0]["tasks"][0]
    action = {
        "reading": "先读一节",
        "review": "先回顾",
        "practice": "先做一组练习",
    }.get(first["task_type"], "先完成")
    text = f"今天大概需要 {minutes} 分钟。{action}「{first['title']}」"
    if sum(group["task_count"] for group in groups) > 1:
        text += "，再完成其余任务"
    return text + "。"


def render_email(digest: dict) -> dict:
    target = date.fromisoformat(digest["date"])
    month_day = f"{target.month} 月 {target.day} 日"
    if digest["task_count"]:
        subject = f"循知 · {month_day}学习提醒（{digest['task_count']} 项）"
    else:
        subject = f"循知 · {month_day}学习提醒"
    lines = [
        digest["summary"],
        "",
    ]
    for group in digest["groups"]:
        lines.append(f"【{group['title']}】")
        for item in group["tasks"]:
            lines.append(
                f"- {item['type_label']}：{item['title']}（约 {item['minutes']} 分钟）"
            )
        lines.append("")
    lines.append(ENTRY_HINT)
    text_body = "\n".join(lines).strip() + "\n"
    sections = []
    for group in digest["groups"]:
        items = "".join(
            f"<li><strong>{html.escape(item['type_label'])}</strong>："
            f"{html.escape(item['title'])}"
            + (f"（约 {item['minutes']} 分钟）" if item["minutes"] else "")
            + "</li>"
            for item in group["tasks"]
        )
        sections.append(
            f"<h3 style=\"margin:18px 0 6px;font-size:15px\">{html.escape(group['title'])}</h3>"
            f"<ul style=\"margin:0;padding-left:20px;line-height:1.7\">{items}</ul>"
        )
    html_body = (
        "<div style=\"font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
        "color:#1d2430;line-height:1.65;max-width:560px;margin:0 auto;padding:18px\">"
        f"<h2 style=\"margin:0 0 8px;font-size:20px\">{html.escape(month_day)}学习提醒</h2>"
        f"<p style=\"margin:0 0 12px\">{html.escape(digest['summary'])}</p>"
        + "".join(sections)
        + "<p style=\"margin:22px 0 0;padding-top:12px;border-top:1px solid #e2e6ec;"
        "color:#687182;font-size:13px\">打开循知 → 学习日历</p></div>"
    )
    return {"subject": subject, "text": text_body, "html": html_body}


def preview(db: Session, target: date | None = None) -> dict:
    target = target or schedule_service.today_local()
    digest = daily_digest(db, target)
    message = render_email(digest)
    return {
        "date": target.isoformat(),
        "task_count": digest["task_count"],
        "minutes": digest["minutes"],
        "summary": digest["summary"],
        "groups": digest["groups"],
        **message,
    }


def _config_complete(config: dict, password: str) -> tuple[bool, str]:
    if not config.get("email"):
        return False, "请先填写收件邮箱。"
    if not _parse_time(config.get("time") or ""):
        return False, "提醒时间需要 HH:MM 格式。"
    if not config.get("smtp_host"):
        return False, "请先填写 SMTP 服务器。"
    if not config.get("smtp_port"):
        return False, "请先填写 SMTP 端口。"
    if config.get("smtp_username") and not password:
        return False, "请先保存 SMTP 密码或授权码。"
    return True, ""


def _smtp_config(config: dict) -> mailer.SmtpConfig:
    return mailer.SmtpConfig(
        host=config.get("smtp_host") or "",
        port=int(config.get("smtp_port") or 587),
        encryption=config.get("smtp_encryption") or "starttls",
        username=config.get("smtp_username") or "",
    )


def _next_check(config: dict, now: datetime, *, sent_today: bool = False) -> str | None:
    if not config.get("enabled"):
        return None
    reminder_time = _parse_time(config.get("time") or "")
    if reminder_time is None:
        return None
    if sent_today:
        tomorrow = now.date() + timedelta(days=1)
        return datetime.combine(tomorrow, reminder_time).replace(microsecond=0).isoformat()
    if now.time() < reminder_time:
        return datetime.combine(now.date(), reminder_time).replace(microsecond=0).isoformat()
    return (now + timedelta(minutes=5)).replace(microsecond=0).isoformat()


def _within_window(now: datetime, config: dict) -> bool:
    reminder_time = _parse_time(config.get("time") or "")
    return reminder_time is not None and now.time() >= reminder_time


def claim_delivery(db: Session, target: date, task_count: int, minutes: int, now: datetime) -> tuple[bool, str]:
    key = _delivery_key(target)
    row = db.get(AppSetting, key)
    if row is None:
        payload = {
            "state": "sending",
            "attempts": 1,
            "claimed_at": now.isoformat(),
            "task_count": task_count,
            "minutes": minutes,
        }
        db.add(AppSetting(key=key, value=json.dumps(payload, ensure_ascii=False), updated_at=now_iso()))
        try:
            db.commit()
            return True, "sending"
        except IntegrityError:
            db.rollback()
            return False, "sending"
    try:
        delivery = json.loads(row.value or "{}")
    except (TypeError, ValueError):
        delivery = {}
    state = delivery.get("state") or "failed"
    if state in {"sent", "unconfirmed"}:
        return False, state
    if state == "sending":
        claimed_at = delivery.get("claimed_at")
        try:
            age = now - datetime.fromisoformat(claimed_at)
        except (TypeError, ValueError):
            age = timedelta(0)
        if age < timedelta(minutes=15):
            return False, "sending"
        delivery["state"] = "unconfirmed"
        delivery["error"] = "上次发送过程未完成，结果待确认。"
        row.value = json.dumps(delivery, ensure_ascii=False)
        row.updated_at = now_iso()
        db.commit()
        return False, "unconfirmed"
    attempts = int(delivery.get("attempts") or 0)
    if attempts >= 3:
        return False, "failed"
    delivery.update(
        {
            "state": "sending",
            "attempts": attempts + 1,
            "claimed_at": now.isoformat(),
            "task_count": task_count,
            "minutes": minutes,
        }
    )
    row.value = json.dumps(delivery, ensure_ascii=False)
    row.updated_at = now_iso()
    db.commit()
    return True, "sending"


def finish_delivery(
    db: Session,
    target: date,
    result: dict,
    task_count: int,
    minutes: int,
    now: datetime,
) -> dict:
    key = _delivery_key(target)
    row = db.get(AppSetting, key)
    try:
        delivery = json.loads(row.value or "{}") if row is not None else {}
    except (TypeError, ValueError):
        delivery = {}
    if result.get("ok"):
        state = "sent"
    elif result.get("uncertain"):
        state = "unconfirmed"
    else:
        state = "failed"
    delivery.update(
        {
            "state": state,
            "attempts": int(delivery.get("attempts") or 1),
            "task_count": task_count,
            "minutes": minutes,
            "error": None if result.get("ok") else result.get("message"),
            "finished_at": now.isoformat(),
        }
    )
    if row is None:
        db.add(AppSetting(key=key, value=json.dumps(delivery, ensure_ascii=False), updated_at=now_iso()))
    else:
        row.value = json.dumps(delivery, ensure_ascii=False)
        row.updated_at = now_iso()
    db.commit()
    return delivery


def run_check(db: Session, now: datetime | None = None) -> dict:
    now = now or local_now()
    target = now.date()
    config = load_config(db)
    password = credentials.email_password(workspace.current_workspace())
    if not config.get("enabled"):
        save_status(
            db,
            {
                "state": "not_enabled",
                "last_check_at": now.isoformat(),
                "task_count": 0,
                "minutes": 0,
                "error": None,
                "next_check_at": None,
            },
        )
        return load_status(db)
    complete, reason = _config_complete(config, password)
    if not complete:
        save_status(
            db,
            {
                "state": "config_incomplete",
                "last_check_at": now.isoformat(),
                "task_count": 0,
                "minutes": 0,
                "error": reason,
                "next_check_at": _next_check(config, now),
            },
        )
        return load_status(db)
    if not _within_window(now, config):
        save_status(
            db,
            {
                "state": "waiting",
                "last_check_at": now.isoformat(),
                "task_count": 0,
                "minutes": 0,
                "error": None,
                "next_check_at": _next_check(config, now),
            },
        )
        return load_status(db)
    digest = daily_digest(db, target)
    if digest["task_count"] == 0:
        save_status(
            db,
            {
                "state": "no_tasks",
                "last_check_at": now.isoformat(),
                "task_count": 0,
                "minutes": 0,
                "error": None,
                "next_check_at": _next_check(config, now),
            },
        )
        return load_status(db)
    claimed, claim_state = claim_delivery(
        db, target, digest["task_count"], digest["minutes"], now
    )
    if not claimed:
        save_status(
            db,
            {
                "state": claim_state,
                "last_check_at": now.isoformat(),
                "task_count": digest["task_count"],
                "minutes": digest["minutes"],
                "error": None,
                "next_check_at": _next_check(config, now, sent_today=claim_state == "sent"),
            },
        )
        return load_status(db)
    message = render_email(digest)
    result = mailer.send_email(
        _smtp_config(config),
        password,
        config["email"],
        message["subject"],
        message["text"],
        message["html"],
    )
    finish_delivery(db, target, result, digest["task_count"], digest["minutes"], now)
    state = "sent" if result.get("ok") else ("unconfirmed" if result.get("uncertain") else "failed")
    save_status(
        db,
        {
            "state": state,
            "last_check_at": now.isoformat(),
            "last_result_at": now.isoformat(),
            "task_count": digest["task_count"],
            "minutes": digest["minutes"],
            "error": None if result.get("ok") else result.get("message"),
            "next_check_at": _next_check(config, now, sent_today=result.get("ok")),
        },
    )
    logger.info(
        "reminder daily state=%s tasks=%s minutes=%s",
        state,
        digest["task_count"],
        digest["minutes"],
    )
    return load_status(db)


def send_test_email(
    db: Session,
    config: dict,
    confirm_email: str,
    now: datetime | None = None,
) -> dict:
    now = now or local_now()
    email = str(config.get("email") or "").strip()
    if not email or str(confirm_email or "").strip() != email:
        return {"ok": False, "code": "email_confirm_required", "message": "请先确认当前收件地址。"}
    password = credentials.email_password(workspace.current_workspace())
    complete, reason = _config_complete(config, password)
    if not complete:
        return {"ok": False, "code": "config_incomplete", "message": reason}
    subject = "循知 · 提醒测试邮件"
    text = "这是一封循知学习提醒测试邮件。发送成功说明当前 SMTP 配置可用。\n\n" + ENTRY_HINT + "\n"
    html_body = (
        "<p>这是一封循知学习提醒测试邮件。</p>"
        "<p>发送成功说明当前 SMTP 配置可用。</p>"
        f"<p style=\"color:#687182\">{ENTRY_HINT}</p>"
    )
    result = mailer.send_email(
        _smtp_config(config), password, email, subject, text, html_body
    )
    status = load_status(db)
    last_test = {
        "state": "sent" if result.get("ok") else ("unconfirmed" if result.get("uncertain") else "failed"),
        "at": now.isoformat(),
        "error": None if result.get("ok") else result.get("message"),
        "code": result.get("code"),
    }
    status["last_test"] = last_test
    save_status(db, {"last_test": last_test})
    return result

def expected_next_check(config: dict, status: dict, now: datetime | None = None) -> str | None:
    now = now or local_now()
    sent_today = (
        status.get("state") == "sent"
        and str(status.get("last_result_at") or "").startswith(now.date().isoformat())
    )
    return _next_check(config, now, sent_today=sent_today)


def config_complete(config: dict, password: str) -> tuple[bool, str]:
    return _config_complete(config, password)
