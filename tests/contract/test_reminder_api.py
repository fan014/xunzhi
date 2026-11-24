from __future__ import annotations

import json
import time
from datetime import date, datetime, time as dt_time

from sqlalchemy import delete, select

from app.api import settings as settings_api
from app.config import settings
from app.domain import reminder
from app.domain.models import AppSetting, ScheduleTask
from app.infra import credentials, workspace
from app.infra.db import SessionLocal
from tests.contract.test_schedule_api import _create_plan_pack, _schedule_plan


def _reset_reminder(client):
    with SessionLocal() as db:
        db.execute(
            delete(AppSetting).where(
                (AppSetting.key.in_([reminder.CONFIG_KEY, reminder.STATUS_KEY]))
                | (AppSetting.key.like("reminder.delivery.%"))
            )
        )
        db.commit()
    credentials.reset_email_password(workspace.current_workspace())


def _config(**overrides):
    values = {
        "enabled": False,
        "email": "student@example.com",
        "time": "00:00",
        "smtp_host": "127.0.0.1",
        "smtp_port": 2525,
        "smtp_encryption": "none",
        "smtp_username": "",
    }
    values.update(overrides)
    return values


def _save_config(**overrides):
    with SessionLocal() as db:
        reminder.save_config(db, _config(**overrides))


def _today_task(client, tmp_path):
    plan_id, version_id, _pack_id, _questions = _create_plan_pack(
        client, tmp_path, "提醒测试计划", module_count=1
    )
    _schedule_plan(client, plan_id, version_id, start=date.today().isoformat())
    with SessionLocal() as db:
        task = db.execute(
            select(ScheduleTask)
            .where(
                ScheduleTask.scheduled_date == date.today().isoformat(),
                ScheduleTask.status == "pending",
            )
            .order_by(ScheduleTask.ordinal)
        ).scalars().first()
    assert task is not None
    return task


def test_reminder_settings_preview_password_and_enable_pause(client, tmp_path, monkeypatch):
    _reset_reminder(client)
    _today_task(client, tmp_path)
    initial = client.get("/api/settings/reminder").json()
    assert initial["config"]["enabled"] is False
    assert initial["password"]["configured"] is False
    assert initial["next_check_at"] is None

    saved = client.put(
        "/api/settings/reminder",
        json={
            "email": "student@example.com",
            "time": "20:00",
            "smtp_host": "127.0.0.1",
            "smtp_port": 2525,
            "smtp_encryption": "none",
            "smtp_username": "",
            "password": "mail-secret-123456",
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["password"]["configured"] is True
    assert "mail-secret-123456" not in json.dumps(body, ensure_ascii=False)
    assert "mail-secret-123456" not in (
        workspace.current_workspace() / "email_secrets.json"
    ).read_text(encoding="utf-8")

    preview = client.post("/api/settings/reminder/preview")
    assert preview.status_code == 200
    content = preview.json()
    assert content["task_count"] >= 1
    assert "打开循知 → 学习日历" in content["text"]
    assert "正确率" not in content["text"]

    calls = []
    monkeypatch.setattr(
        reminder.mailer,
        "send_email",
        lambda *args, **kwargs: calls.append(args) or {
            "ok": True,
            "code": "sent",
            "message": "邮件服务已接收。",
        },
    )
    tested = client.post(
        "/api/settings/reminder/test",
        json={"confirm_email": "student@example.com"},
    )
    assert tested.status_code == 200, tested.text
    assert tested.json()["result"]["ok"] is True
    with SessionLocal() as db:
        deliveries = db.execute(
            select(AppSetting).where(AppSetting.key.like("reminder.delivery.%"))
        ).scalars().all()
        assert deliveries == []
        status = reminder.load_status(db)
        assert status["last_test"]["state"] == "sent"

    monkeypatch.setattr(
        settings_api.reminder_scheduler,
        "register",
        lambda *args, **kwargs: {"ok": True, "state": "registered", "message": "已注册"},
    )
    enabled = client.post("/api/settings/reminder/enable")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["config"]["enabled"] is True
    assert enabled.json()["scheduler"]["ok"] is True

    monkeypatch.setattr(
        settings_api.reminder_scheduler,
        "unregister",
        lambda *args, **kwargs: {"ok": True, "state": "removed", "message": "已移除"},
    )
    paused = client.post("/api/settings/reminder/pause")
    assert paused.status_code == 200
    assert paused.json()["config"]["enabled"] is False
    assert paused.json()["scheduler"]["ok"] is True
    _reset_reminder(client)


def test_daily_delivery_sends_once_and_paused_skips(client, tmp_path, monkeypatch):
    _reset_reminder(client)
    _today_task(client, tmp_path)
    _save_config(enabled=True)
    calls = []
    monkeypatch.setattr(
        reminder.mailer,
        "send_email",
        lambda *args, **kwargs: calls.append(args) or {
            "ok": True,
            "code": "sent",
            "message": "邮件服务已接收。",
        },
    )
    now = datetime.combine(date.today(), dt_time(22, 0))
    with SessionLocal() as db:
        first = reminder.run_check(db, now=now)
        second = reminder.run_check(db, now=now)
    assert first["state"] == "sent"
    assert second["state"] == "sent"
    assert len(calls) == 1

    _save_config(enabled=False)
    with SessionLocal() as db:
        paused = reminder.run_check(db, now=now)
    assert paused["state"] == "not_enabled"
    assert len(calls) == 1
    _reset_reminder(client)


def test_digest_uses_latest_pending_tasks_and_groups_multiple_plans(client, tmp_path):
    from app.domain import reminder

    _reset_reminder(client)
    plan1, version1, _p1, _q1 = _create_plan_pack(client, tmp_path, "提醒多计划一", module_count=1)
    plan2, version2, _p2, _q2 = _create_plan_pack(client, tmp_path, "提醒多计划二", module_count=1)
    _schedule_plan(client, plan1, version1, start=date.today().isoformat())
    _schedule_plan(client, plan2, version2, start=date.today().isoformat())
    with SessionLocal() as db:
        digest = reminder.daily_digest(db, date.today())
        assert len(digest["groups"]) >= 2
        today_tasks = [
            task
            for group in digest["groups"]
            for task in group["tasks"]
            if task["task_type"] == "reading"
        ]
        assert len(today_tasks) >= 2
        first_id = today_tasks[0]["id"]
        second_id = today_tasks[1]["id"]
        first = db.get(ScheduleTask, first_id)
        second = db.get(ScheduleTask, second_id)
        first.status = "completed"
        second.scheduled_date = (date.today()).isoformat()
        second.status = "pending"
        db.commit()
        moved_date = (date.today().replace(day=date.today().day) + __import__("datetime").timedelta(days=1)).isoformat()
        second.scheduled_date = moved_date
        db.commit()
        latest = reminder.daily_digest(db, date.today())
        remaining_ids = {
            task["id"] for group in latest["groups"] for task in group["tasks"]
        }
        assert first_id not in remaining_ids
        assert second_id not in remaining_ids
    _reset_reminder(client)
