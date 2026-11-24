from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta

from sqlalchemy import select

from app.domain import schedule as schedule_service
from app.domain.models import (
    Attempt,
    LearningPack,
    LearningPlanVersion,
    PackPlanVersion,
    Question,
    ScheduleTask,
    now_iso,
)
from app.infra.db import SessionLocal
from app.infra.ids import new_id
from tests import stage4_helpers as helpers


def _modules():
    return [
        {
            "module_id": "mod_shift_a",
            "title": "模块甲",
            "knowledge_point_ids": ["kp_shift_a1", "kp_shift_a2"],
            "core_points": ["概念甲一", "概念甲二"],
            "suggested_question_count": 4,
            "source_names": ["计划测试资料"],
        },
        {
            "module_id": "mod_shift_b",
            "title": "模块乙",
            "knowledge_point_ids": ["kp_shift_b1", "kp_shift_b2"],
            "core_points": ["概念乙一", "概念乙二"],
            "suggested_question_count": 4,
            "source_names": ["计划测试资料"],
        },
    ]


def _create_plan_pack(client, tmp_path, name: str):
    source = helpers.upload(client, helpers.make_png(tmp_path / f"{name}_{time.time_ns()}.png"))
    response = client.post(
        "/api/plans",
        json={
            "title": name,
            "sources": [{"source_id": source["id"], "page_start": None, "page_end": None}],
            "goal": "按节奏学完两个模块",
            "points": [],
            "question_count": 8,
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    version_id = plan["current_version_id"]
    with SessionLocal() as db:
        version = db.get(LearningPlanVersion, version_id)
        version.modules_json = json.dumps(_modules(), ensure_ascii=False)
        version.question_count = 8
        db.commit()
    confirmed = client.post(f"/api/plans/{plan['id']}/versions/{version_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text

    modules = _modules()
    with SessionLocal() as db:
        pack = LearningPack(
            id=new_id("pack"),
            display_title=f"{name}学习内容",
            source_kind="real",
            difficulty="consolidate",
            question_count=8,
            job_id=f"job_shift_{time.time_ns()}",
            handout_word_count=0,
            model_name="test",
            prompt_version="test",
            created_at=now_iso(),
        )
        db.add(pack)
        db.flush()
        questions = []
        ordinal = 1
        module_of_question = {}
        for module in modules:
            for index in range(4):
                stem = f"{module['title']} 第 {index + 1} 题"
                question = Question(
                    id=new_id("question"),
                    pack_id=pack.id,
                    ordinal=ordinal,
                    stem=stem,
                    options_json=json.dumps(
                        [
                            {"key": "A", "text": "选项 A"},
                            {"key": "B", "text": "选项 B"},
                            {"key": "C", "text": "选项 C"},
                            {"key": "D", "text": "选项 D"},
                        ],
                        ensure_ascii=False,
                    ),
                    answer_index=0,
                    explanation="测试解析",
                    knowledge_point=module["title"],
                    module_id=module["module_id"],
                    difficulty_tag="consolidate",
                    is_image_based=False,
                    stem_hash=hashlib.sha256(stem.encode("utf-8")).hexdigest(),
                    created_at=now_iso(),
                )
                db.add(question)
                questions.append(question)
                module_of_question[question.id] = module["module_id"]
                ordinal += 1
        db.add(
            PackPlanVersion(
                pack_id=pack.id,
                plan_version_id=version_id,
                adopted_at=now_iso(),
            )
        )
        db.commit()
    return plan["id"], version_id, pack.id, questions, module_of_question


def _schedule(client, plan_id, version_id, start="2030-08-01"):
    response = client.post(
        f"/api/plans/{plan_id}/versions/{version_id}/schedule",
        json={"start_date": start, "duration_days": 21, "daily_minutes": 30},
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        return list(
            db.execute(
                select(ScheduleTask)
                .where(ScheduleTask.plan_version_id == version_id)
                .order_by(ScheduleTask.ordinal)
            ).scalars().all()
        )


def _submit_group(client, pack_id, task, questions):
    page = client.get(f"/api/packs/{pack_id}?task_id={task.id}")
    assert page.status_code == 200, page.text
    ids = {item["id"] for item in page.json()["questions"]}
    answers = [
        {"question_id": question.id, "selected_index": 0}
        for question in questions
        if question.id in ids
    ]
    response = client.post(
        f"/api/packs/{pack_id}/submit",
        json={
            "idempotency_key": f"shift-submit-{time.time_ns()}",
            "task_id": task.id,
            "answers": answers,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _complete_tasks(client, tasks):
    for task in tasks:
        if task.task_type == "practice":
            continue
        response = client.post(f"/api/schedule/tasks/{task.id}/complete")
        assert response.status_code == 200, response.text


def test_shift_suggestion_preview_confirm_and_duplicate(client, tmp_path, monkeypatch):
    plan_id, version_id, pack_id, questions, _ = _create_plan_pack(
        client, tmp_path, "前移测试计划"
    )
    tasks = _schedule(client, plan_id, version_id, start="2030-08-01")
    by_date: dict[str, list[ScheduleTask]] = {}
    for task in tasks:
        by_date.setdefault(task.scheduled_date, []).append(task)
    day1 = by_date["2030-08-01"]
    day2 = by_date["2030-08-02"]
    practice = next(task for task in day2 if task.task_type == "practice")
    b_reading = next(
        task
        for task in tasks
        if task.module_id == "mod_shift_b" and task.task_type == "reading"
    )
    b_practice = next(
        task
        for task in tasks
        if task.module_id == "mod_shift_b" and task.task_type == "practice"
    )
    assert b_reading.scheduled_date == "2030-08-03"
    assert b_practice.scheduled_date == "2030-08-04"
    _complete_tasks(client, day1 + [task for task in day2 if task.task_type != "practice"])
    _submit_group(client, pack_id, practice, questions)

    monkeypatch.setattr(schedule_service, "today_local", lambda: date(2030, 8, 1))
    calendar = client.get(f"/api/schedule?plan_id={plan_id}&date=2030-08-01").json()
    assert calendar["shift"]["available"] is True
    assert calendar["shift"]["shift_days"] == 1
    assert calendar["shift"]["ahead_dates"] == ["2030-08-02"]
    preview_key = calendar["shift"]["preview_key"]
    assert {item["to_date"] for item in calendar["shift"]["changes"]} <= {
        "2030-08-02",
        "2030-08-03",
        "2030-08-04",
    }

    preview = client.post(
        "/api/schedule/shift/preview",
        json={"plan_id": plan_id, "version_id": version_id},
    )
    assert preview.status_code == 200, preview.text
    changes = preview.json()["preview"]["changes"]
    assert ("2030-08-03" in {item["from_date"] for item in changes})
    assert any(
        item["from_date"] == "2030-08-03" and item["to_date"] == "2030-08-02"
        for item in changes
    )

    confirmed = client.post(
        "/api/schedule/shift/confirm",
        json={"plan_id": plan_id, "version_id": version_id, "preview_key": preview_key},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["duplicate"] is False
    assert confirmed.json()["applied"] == len(changes)
    with SessionLocal() as db:
        moved = db.get(ScheduleTask, day1[0].id)
        assert moved.scheduled_date == "2030-08-01"
        assert db.get(ScheduleTask, practice.id).scheduled_date == "2030-08-02"
        assert db.get(ScheduleTask, b_reading.id).scheduled_date == "2030-08-02"
        assert db.get(ScheduleTask, b_practice.id).scheduled_date == "2030-08-03"
        rows = db.execute(
            select(ScheduleTask)
            .where(ScheduleTask.plan_version_id == version_id)
            .order_by(ScheduleTask.ordinal)
        ).scalars().all()
        dates = {row.scheduled_date for row in rows}
        assert "2030-08-04" not in dates

    repeated = client.post(
        "/api/schedule/shift/confirm",
        json={"plan_id": plan_id, "version_id": version_id, "preview_key": preview_key},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["duplicate"] is True
    assert repeated.json()["applied"] == 0

    after = client.get(f"/api/schedule?plan_id={plan_id}&date=2030-08-01").json()
    assert after["shift"]["available"] is False

    stale = client.post(
        "/api/schedule/shift/confirm",
        json={"plan_id": plan_id, "version_id": version_id, "preview_key": "stale-key"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "schedule_shift_not_available"


def test_partial_ahead_day_does_not_shift_and_keeps_single_completion(client, tmp_path, monkeypatch):
    plan_id, version_id, _, _, _ = _create_plan_pack(client, tmp_path, "部分前移计划")
    tasks = _schedule(client, plan_id, version_id, start="2030-09-01")
    by_date: dict[str, list[ScheduleTask]] = {}
    for task in tasks:
        by_date.setdefault(task.scheduled_date, []).append(task)
    day2 = by_date["2030-09-02"]
    assert len(day2) >= 2
    only = next(task for task in day2 if task.task_type != "practice")
    assert any(task.task_type == "practice" for task in day2)
    marked = client.post(f"/api/schedule/tasks/{only.id}/complete")
    assert marked.status_code == 200, marked.text
    assert marked.json()["task"]["status"] == "completed"
    assert marked.json()["task"]["completed_at"]

    monkeypatch.setattr(schedule_service, "today_local", lambda: date(2030, 9, 1))
    calendar = client.get(f"/api/schedule?plan_id={plan_id}&date=2030-09-01").json()
    assert calendar["shift"]["available"] is False
    with SessionLocal() as db:
        stored = db.get(ScheduleTask, only.id)
        assert stored.status == "completed"
        assert stored.completed_at
        pending = db.execute(
            select(ScheduleTask).where(
                ScheduleTask.plan_version_id == version_id,
                ScheduleTask.status == "pending",
            )
        ).scalars().all()
        assert any(row.scheduled_date == "2030-09-02" for row in pending)


def test_dismiss_shift_prevents_repeat_prompt(client, tmp_path, monkeypatch):
    plan_id, version_id, pack_id, questions, _ = _create_plan_pack(
        client, tmp_path, "保持原安排计划"
    )
    tasks = _schedule(client, plan_id, version_id, start="2030-10-01")
    by_date: dict[str, list[ScheduleTask]] = {}
    for task in tasks:
        by_date.setdefault(task.scheduled_date, []).append(task)
    practice = next(task for task in by_date["2030-10-02"] if task.task_type == "practice")
    _complete_tasks(
        client,
        by_date["2030-10-01"] + [task for task in by_date["2030-10-02"] if task.task_type != "practice"],
    )
    _submit_group(client, pack_id, practice, questions)
    monkeypatch.setattr(schedule_service, "today_local", lambda: date(2030, 10, 1))

    calendar = client.get(f"/api/schedule?plan_id={plan_id}&date=2030-10-01").json()
    assert calendar["shift"]["available"] is True
    dismissed = client.post(
        "/api/schedule/shift/dismiss",
        json={"plan_id": plan_id, "version_id": version_id, "preview_key": calendar["shift"]["preview_key"]},
    )
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["dismissed"] is True

    refreshed = client.get(f"/api/schedule?plan_id={plan_id}&date=2030-10-01").json()
    assert refreshed["shift"]["available"] is False
    assert refreshed["shift"].get("dismissed") is True


def test_manual_reschedule_preview_confirm_and_stale(client, tmp_path, monkeypatch):
    plan_id, version_id, _, _, _ = _create_plan_pack(client, tmp_path, "改期计划")
    tasks = _schedule(client, plan_id, version_id, start="2030-06-01")
    target = next(task for task in tasks if task.scheduled_date == "2030-06-01" and task.task_type == "reading")
    monkeypatch.setattr(schedule_service, "today_local", lambda: date(2030, 6, 1))

    preview = client.post(
        f"/api/schedule/tasks/{target.id}/reschedule/preview",
        json={"scheduled_date": "2030-06-03"},
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()["preview"]
    assert data["from_date"] == "2030-06-01"
    assert data["to_date"] == "2030-06-03"
    assert data["target_day_minutes"] >= data["estimated_minutes"]
    assert data["over_capacity"] is True

    past = client.post(
        f"/api/schedule/tasks/{target.id}/reschedule",
        json={"scheduled_date": "2030-05-31", "expected_date": "2030-06-01"},
    )
    assert past.status_code == 422, past.text
    assert past.json()["error"]["code"] == "schedule_target_past"

    moved = client.post(
        f"/api/schedule/tasks/{target.id}/reschedule",
        json={"scheduled_date": "2030-06-03", "expected_date": "2030-06-01"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["moved"] is True
    assert "6 月 3 日" in moved.json()["message"]
    with SessionLocal() as db:
        stored = db.get(ScheduleTask, target.id)
        assert stored.scheduled_date == "2030-06-03"
        assert stored.id == target.id
        assert stored.status == "pending"

    duplicate = client.post(
        f"/api/schedule/tasks/{target.id}/reschedule",
        json={"scheduled_date": "2030-06-03", "expected_date": "2030-06-01"},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["duplicate"] is True

    stale = client.post(
        f"/api/schedule/tasks/{target.id}/reschedule",
        json={"scheduled_date": "2030-06-05", "expected_date": "2030-06-01"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "schedule_task_changed"


def test_reschedule_rejects_completed_task(client, tmp_path):
    plan_id, version_id, _, _, _ = _create_plan_pack(client, tmp_path, "已完成改期计划")
    tasks = _schedule(client, plan_id, version_id, start="2030-07-01")
    reading = next(task for task in tasks if task.task_type == "reading")
    client.post(f"/api/schedule/tasks/{reading.id}/complete")
    response = client.post(
        f"/api/schedule/tasks/{reading.id}/reschedule/preview",
        json={"scheduled_date": "2030-07-05"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "schedule_task_completed"


def test_daily_brief_counts_tasks_and_submission_once(client, tmp_path):
    plan_id, version_id, pack_id, questions, module_of_question = _create_plan_pack(
        client, tmp_path, "简报计划"
    )
    tasks = _schedule(client, plan_id, version_id, start="2030-07-01")
    day1 = [task for task in tasks if task.scheduled_date == "2030-07-01"]
    _complete_tasks(client, [task for task in day1 if task.task_type != "practice"])

    answers = [
        {"question_id": question.id, "selected_index": 0}
        for question in questions
    ]
    submitted = client.post(
        f"/api/packs/{pack_id}/submit",
        json={"idempotency_key": f"brief-whole-{time.time_ns()}", "answers": answers},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["score"]["total"] == 8

    today = str(date.today())
    brief = client.get(
        f"/api/schedule/brief?plan_id={plan_id}&version_id={version_id}&date={today}"
    )
    assert brief.status_code == 200, brief.text
    data = brief.json()
    assert data["practice"]["total_attempts"] == 1
    assert data["practice"]["total_questions"] == 8
    assert data["practice"]["answered"] == 8
    assert data["practice"]["correct"] == 8
    assert data["counts"]["ahead"] >= 1
    assert "提前学的" in data["summary"]
    assert "答对了 8 题" in data["summary"]
    assert data["next_step"]

    empty = client.get(
        f"/api/schedule/brief?plan_id={plan_id}&version_id={version_id}&date=2030-07-15"
    ).json()
    assert empty["empty"] is True
    assert empty["practice"]["total_attempts"] == 0


def test_brief_local_date_handles_utc_submission():
    from app.domain.brief import local_date_of

    assert local_date_of("2026-11-24T16:30:00Z") is not None
    assert local_date_of("2026-11-24T16:30:00") == date(2026, 11, 24)
    assert local_date_of("2026-11-24") == date(2026, 11, 24)