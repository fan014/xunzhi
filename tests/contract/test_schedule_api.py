from __future__ import annotations

import hashlib
import json
import time
from datetime import date

from sqlalchemy import select

from app.domain.models import (
    HandoutSection,
    LearningEvidence,
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


def _modules(module_count: int = 2):
    modules = [
        {
            "module_id": "mod_cal_a",
            "title": "模块甲",
            "knowledge_point_ids": ["kp_cal_a1", "kp_cal_a2"],
            "core_points": ["概念甲一", "概念甲二"],
            "suggested_question_count": 4,
            "source_names": ["日历测试资料"],
        },
        {
            "module_id": "mod_cal_b",
            "title": "模块乙",
            "knowledge_point_ids": ["kp_cal_b1", "kp_cal_b2"],
            "core_points": ["概念乙一", "概念乙二"],
            "suggested_question_count": 4,
            "source_names": ["日历测试资料"],
        },
    ]
    for index in range(3, module_count + 1):
        modules.append(
            {
                "module_id": f"mod_cal_{index}",
                "title": f"模块{index}",
                "knowledge_point_ids": [f"kp_cal_{index}_1", f"kp_cal_{index}_2"],
                "core_points": [f"概念{index}一", f"概念{index}二"],
                "suggested_question_count": 4,
                "source_names": ["日历测试资料"],
            }
        )
    return modules[:module_count]


def _create_plan(client, tmp_path, name: str = "日历测试计划", module_count: int = 2):
    source = helpers.upload(client, helpers.make_png(tmp_path / f"{name}_{time.time_ns()}.png"))
    count = module_count * 4
    response = client.post(
        "/api/plans",
        json={
            "title": name,
            "sources": [{"source_id": source["id"], "page_start": None, "page_end": None}],
            "goal": "按七天节奏学完两个模块",
            "points": [],
            "question_count": count,
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    version_id = plan["current_version_id"]
    with SessionLocal() as db:
        version = db.get(LearningPlanVersion, version_id)
        version.modules_json = json.dumps(_modules(module_count), ensure_ascii=False)
        version.question_count = count
        db.commit()
    confirmed = client.post(f"/api/plans/{plan['id']}/versions/{version_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return source, plan["id"], version_id


def _preview(client, version_id, **options):
    body = {"plan_version_id": version_id, **options}
    response = client.post("/api/schedule/preview", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _create_plan_pack(
    client, tmp_path, name: str, module_count: int = 3, pack_module_count: int | None = None
):
    _, plan_id, version_id = _create_plan(
        client, tmp_path, name, module_count=module_count
    )
    modules = _modules(module_count)
    pack_modules = modules[: (pack_module_count or module_count)]
    with SessionLocal() as db:
        pack = LearningPack(
            id=new_id("pack"),
            display_title=f"{name}学习内容",
            source_kind="real",
            difficulty="consolidate",
            question_count=module_count * 4,
            job_id=f"job_{time.time_ns()}",
            handout_word_count=0,
            model_name="test",
            prompt_version="test",
            created_at=now_iso(),
        )
        db.add(pack)
        db.flush()
        ordinal = 1
        questions = []
        for module in pack_modules:
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
                ordinal += 1
        db.add(
            PackPlanVersion(
                pack_id=pack.id,
                plan_version_id=version_id,
                adopted_at=now_iso(),
            )
        )
        db.commit()
    return plan_id, version_id, pack.id, questions


def _schedule_plan(client, plan_id: str, version_id: str, start: str = "2030-08-01"):
    response = client.post(
        f"/api/plans/{plan_id}/versions/{version_id}/schedule",
        json={"start_date": start, "duration_days": 14, "daily_minutes": 30},
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        return list(
            db.execute(
                select(ScheduleTask)
                .where(
                    ScheduleTask.plan_version_id == version_id,
                    ScheduleTask.task_type == "practice",
                )
                .order_by(ScheduleTask.ordinal)
            ).scalars().all()
        )


def test_schedule_preview_dates_capacity_and_validation(client, tmp_path):
    _, plan_id, version_id = _create_plan(
        client, tmp_path, "日历预览计划", module_count=5
    )
    week = _preview(client, version_id, start_date="2030-01-01", duration_days=7, daily_minutes=30)
    fortnight = _preview(
        client, version_id, start_date="2030-01-01", duration_days=14, daily_minutes=30
    )
    assert week["target_date"] == "2030-01-07"
    assert fortnight["target_date"] == "2030-01-14"
    assert week["task_count"] == fortnight["task_count"]
    assert [task["task_type"] for task in week["tasks"]].count("practice") == 5
    assert week["over_capacity"] is True
    assert fortnight["over_capacity"] is False

    tight = _preview(client, version_id, start_date="2030-01-01", duration_days=2, daily_minutes=15)
    assert tight["shortfall_minutes"] > 0
    assert tight["over_capacity"] is True
    assert tight["capacity_minutes"] == 30

    custom = _preview(
        client, version_id, start_date="2030-02-01", target_date="2030-02-10", daily_minutes=30
    )
    assert custom["duration_days"] == 10
    assert custom["target_date"] == "2030-02-10"

    invalid = client.post(
        "/api/schedule/preview",
        json={"plan_version_id": version_id, "start_date": "2030-02-10", "target_date": "2030-02-01"},
    )
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["error"]["code"] == "schedule_target_before_start"

    no_plan = client.post("/api/schedule/preview", json={"plan_version_id": "missing"})
    assert no_plan.status_code == 404


def test_schedule_save_is_idempotent_and_manual_completion(client, tmp_path):
    _, plan_id, version_id = _create_plan(client, tmp_path, "日历保存计划")
    created = client.post(
        f"/api/plans/{plan_id}/versions/{version_id}/schedule",
        json={"start_date": "2030-06-01", "duration_days": 14, "daily_minutes": 30},
    )
    assert created.status_code == 200, created.text
    first = created.json()
    assert first["duplicate"] is False
    assert first["tasks"]
    first_ids = [task["id"] for task in first["tasks"]]

    again = client.post(
        f"/api/plans/{plan_id}/versions/{version_id}/schedule",
        json={"start_date": "2030-07-01", "duration_days": 7, "daily_minutes": 60},
    )
    assert again.status_code == 200, again.text
    second = again.json()
    assert second["duplicate"] is True
    assert [task["id"] for task in second["tasks"]] == first_ids
    assert second["start_date"] == "2030-06-01"

    calendar = client.get(
        f"/api/schedule?plan_id={plan_id}&month=2030-06&date=2030-06-01"
    )
    assert calendar.status_code == 200, calendar.text
    data = calendar.json()
    assert data["plan"]["id"] == plan_id
    assert data["plan"]["unassigned"] is False
    assert data["selected_date"] == "2030-06-01"
    assert data["selected_tasks"]
    assert data["progress"]["completed"] == 0
    assert "2030-06-01" in data["month_days"]

    reading = next(task for task in data["selected_tasks"] if task["task_type"] == "reading")
    marked = client.post(f"/api/schedule/tasks/{reading['id']}/complete")
    assert marked.status_code == 200, marked.text
    assert marked.json()["duplicate"] is False
    assert marked.json()["progress"]["completed"] == 1

    repeated = client.post(f"/api/schedule/tasks/{reading['id']}/complete")
    assert repeated.status_code == 200
    assert repeated.json()["duplicate"] is True

    with SessionLocal() as db:
        practice = db.execute(
            select(ScheduleTask).where(
                ScheduleTask.plan_version_id == version_id,
                ScheduleTask.task_type == "practice",
            )
        ).scalars().first()
    rejected = client.post(f"/api/schedule/tasks/{practice.id}/complete")
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "practice_requires_submission"


def test_unassigned_legacy_plan_can_be_scheduled_and_keeps_modules(client, tmp_path):
    source, plan_id, version_id = _create_plan(client, tmp_path, "旧计划补排")
    before = client.get(f"/api/schedule?plan_id={plan_id}")
    assert before.status_code == 200, before.text
    assert before.json()["plan"]["unassigned"] is True
    assert before.json()["progress"]["label"] == "未安排日程"
    assert before.json()["plan"]["pack_status"] == "pending"

    scheduled = client.post(
        f"/api/plans/{plan_id}/versions/{version_id}/schedule",
        json={"start_date": "2020-01-01", "duration_days": 7, "daily_minutes": 30},
    )
    assert scheduled.status_code == 200, scheduled.text

    after = client.get(f"/api/schedule?plan_id={plan_id}&date=2020-01-01&month=2020-01")
    assert after.status_code == 200, after.text
    payload = after.json()
    assert payload["plan"]["unassigned"] is False
    assert payload["progress"]["overdue_count"] > 0
    assert all(task["state"] == "overdue" for task in payload["selected_tasks"])

    plan_detail = client.get(f"/api/plans/{plan_id}")
    assert plan_detail.status_code == 200
    version = plan_detail.json()["versions"][0]
    assert version["id"] == version_id
    assert [item["module_id"] for item in version["modules"]] == ["mod_cal_a", "mod_cal_b"]
    assert client.get(f"/api/sources").status_code == 200


def test_scoped_practice_is_independent_and_settles_only_its_group(client, tmp_path):
    plan_id, version_id, pack_id, questions = _create_plan_pack(
        client, tmp_path, "分组练习计划", module_count=3
    )
    tasks = _schedule_plan(client, plan_id, version_id, start="2030-08-01")
    assert len(tasks) == 3
    by_module = {}
    for question in questions:
        by_module.setdefault(question.module_id, []).append(question.id)
    task_a, task_b, task_c = tasks
    ids_a = by_module[task_a.module_id]
    ids_b = by_module[task_b.module_id]
    ids_c = by_module[task_c.module_id]

    scoped = client.get(f"/api/packs/{pack_id}?task_id={task_a.id}")
    assert scoped.status_code == 200, scoped.text
    payload = scoped.json()
    assert payload["question_count"] == 4
    assert payload["full_question_count"] == 12
    assert payload["practice_scope"]["total"] == 4
    assert [item["id"] for item in payload["questions"]] == ids_a

    draft_a = client.put(
        f"/api/packs/{pack_id}/draft?task_id={task_a.id}",
        json={"answers": [{"question_id": ids_a[0], "selected_index": 0}]},
    )
    draft_b = client.put(
        f"/api/packs/{pack_id}/draft?task_id={task_b.id}",
        json={"answers": [{"question_id": ids_b[0], "selected_index": 1}]},
    )
    assert draft_a.status_code == 200, draft_a.text
    assert draft_b.status_code == 200, draft_b.text
    assert len(client.get(f"/api/packs/{pack_id}?task_id={task_a.id}").json()["draft"]["answers"]) == 1
    assert len(client.get(f"/api/packs/{pack_id}?task_id={task_b.id}").json()["draft"]["answers"]) == 1

    partial = client.post(
        f"/api/packs/{pack_id}/submit",
        json={
            "idempotency_key": f"partial-b-{time.time_ns()}",
            "task_id": task_b.id,
            "answers": [{"question_id": ids_b[0], "selected_index": 1}],
        },
    )
    assert partial.status_code == 200, partial.text
    partial_scope = partial.json()["practice_scope"]
    assert partial_scope["completed"] is False
    assert partial_scope["remaining"] == 3
    assert partial.json()["score"]["total"] == 4
    assert partial.json()["score"]["unanswered"] == 3

    b_page = client.get(f"/api/packs/{pack_id}?task_id={task_b.id}").json()
    assert b_page["answer_revealed"] is True
    assert b_page["result"]["score"]["total"] == 4
    assert b_page["practice_scope"]["remaining"] == 3

    wrong_answers = [{"question_id": qid, "selected_index": 1} for qid in ids_a]
    key = f"full-a-{time.time_ns()}"
    submitted = client.post(
        f"/api/packs/{pack_id}/submit",
        json={"idempotency_key": key, "task_id": task_a.id, "answers": wrong_answers},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["practice_scope"]["completed"] is True
    assert submitted.json()["practice_scope"]["completed_ahead"] is True
    assert submitted.json()["score"]["wrong"] == 4
    completed_at = submitted.json()["practice_scope"]["completed_at"]
    with SessionLocal() as db:
        evidence = db.execute(
            select(LearningEvidence).where(
                LearningEvidence.attempt_id == submitted.json()["attempt_id"]
            )
        ).scalars().all()
    assert {row.question_id for row in evidence} == set(ids_a)
    assert all(row.source_type == "practice" for row in evidence)

    duplicate = client.post(
        f"/api/packs/{pack_id}/submit",
        json={"idempotency_key": key, "task_id": task_a.id, "answers": wrong_answers},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["practice_scope"]["completed_at"] == completed_at

    with SessionLocal() as db:
        rows = db.execute(
            select(ScheduleTask)
            .where(
                ScheduleTask.plan_version_id == version_id,
                ScheduleTask.task_type == "practice",
            )
            .order_by(ScheduleTask.ordinal)
        ).scalars().all()
    assert [row.status for row in rows] == ["completed", "pending", "pending"]
    assert rows[0].scheduled_date == task_a.scheduled_date
    assert rows[2].completed_at is None

    wrong_group = client.post(
        f"/api/packs/{pack_id}/submit",
        json={
            "idempotency_key": f"wrong-group-{time.time_ns()}",
            "task_id": task_c.id,
            "answers": [{"question_id": ids_a[0], "selected_index": 0}],
        },
    )
    assert wrong_group.status_code == 422, wrong_group.text
    assert wrong_group.json()["error"]["code"] == "question_not_in_task"

    reattempt_b = client.post(f"/api/packs/{pack_id}/reattempt?task_id={task_b.id}")
    assert reattempt_b.status_code == 201, reattempt_b.text
    resumed = client.get(f"/api/packs/{pack_id}?task_id={task_b.id}").json()
    assert resumed["answer_revealed"] is False
    assert resumed["draft"]["answers"]


def test_whole_pack_submit_completes_only_covered_groups(client, tmp_path):
    plan_id, version_id, pack_id, questions = _create_plan_pack(
        client, tmp_path, "整套覆盖计划", module_count=3
    )
    tasks = _schedule_plan(client, plan_id, version_id)
    by_module = {}
    for question in questions:
        by_module.setdefault(question.module_id, []).append(question.id)
    ids_a = by_module[tasks[0].module_id]
    ids_b = by_module[tasks[1].module_id]
    ids_c = by_module[tasks[2].module_id]

    first = client.post(
        f"/api/packs/{pack_id}/submit",
        json={
            "idempotency_key": f"whole-c-{time.time_ns()}",
            "answers": [{"question_id": qid, "selected_index": 0} for qid in ids_c],
        },
    )
    assert first.status_code == 200, first.text
    with SessionLocal() as db:
        rows = db.execute(
            select(ScheduleTask)
            .where(
                ScheduleTask.plan_version_id == version_id,
                ScheduleTask.task_type == "practice",
            )
            .order_by(ScheduleTask.ordinal)
        ).scalars().all()
    assert [row.status for row in rows] == ["pending", "pending", "completed"]
    assert rows[2].completion_source == "practice"

    reopened = client.post(f"/api/packs/{pack_id}/reattempt")
    assert reopened.status_code == 201, reopened.text
    second = client.post(
        f"/api/packs/{pack_id}/submit",
        json={
            "idempotency_key": f"whole-ab-{time.time_ns()}",
            "answers": [
                {"question_id": qid, "selected_index": 0}
                for qid in ids_a + ids_b
            ],
        },
    )
    assert second.status_code == 200, second.text
    with SessionLocal() as db:
        rows = db.execute(
            select(ScheduleTask)
            .where(
                ScheduleTask.plan_version_id == version_id,
                ScheduleTask.task_type == "practice",
            )
            .order_by(ScheduleTask.ordinal)
        ).scalars().all()
    assert [row.status for row in rows] == ["completed", "completed", "completed"]


def test_practice_task_without_questions_is_unavailable(client, tmp_path):
    plan_id, version_id, pack_id, questions = _create_plan_pack(
        client, tmp_path, "缺题计划", module_count=3, pack_module_count=1
    )
    tasks = _schedule_plan(client, plan_id, version_id)
    assert len(tasks) == 3
    missing = tasks[1]
    calendar = client.get(
        f"/api/schedule?plan_id={plan_id}&date={missing.scheduled_date}&month={missing.scheduled_date[:7]}"
    )
    assert calendar.status_code == 200, calendar.text
    selected = next(item for item in calendar.json()["selected_tasks"] if item["id"] == missing.id)
    assert selected["practice_available"] is False
    assert selected["action_url"] is None
    assert selected["practice_unavailable_reason"]

    scoped = client.get(f"/api/packs/{pack_id}?task_id={missing.id}")
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["practice_scope"]["available"] is False
    assert scoped.json()["questions"] == []
    blocked = client.post(
        f"/api/packs/{pack_id}/submit",
        json={
            "idempotency_key": f"missing-{time.time_ns()}",
            "task_id": missing.id,
            "answers": [],
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "task_questions_unavailable"


def test_task_scope_rejects_cross_plan_access(client, tmp_path):
    plan_a, version_a, pack_a, _ = _create_plan_pack(
        client, tmp_path, "跨计划甲", module_count=1
    )
    tasks_a = _schedule_plan(client, plan_a, version_a)
    plan_b, version_b, pack_b, _ = _create_plan_pack(
        client, tmp_path, "跨计划乙", module_count=1
    )
    tasks_b = _schedule_plan(client, plan_b, version_b)
    assert tasks_a and tasks_b
    response = client.get(f"/api/packs/{pack_b}?task_id={tasks_a[0].id}")
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "task_plan_mismatch"
    submit = client.post(
        f"/api/packs/{pack_b}/submit",
        json={
            "idempotency_key": f"cross-{time.time_ns()}",
            "task_id": tasks_a[0].id,
            "answers": [],
        },
    )
    assert submit.status_code == 409, submit.text


def test_calendar_page_and_sidebar_entry(client):
    page = client.get("/calendar")
    assert page.status_code == 200
    assert "学习日历" in page.text
    home = client.get("/")
    assert home.status_code == 200
    assert "/calendar" in home.text

def test_diagnosis_preview_and_confirm_persist_schedule(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(31, 71, 111), count=3)
    started = client.post(
        f"/api/sources/{source['id']}/diagnosis/sessions",
        json={
            "idempotency_key": f"calendar-diag-{time.time_ns()}",
            "goal": "用一周时间补薄弱点",
            "question_count": 3,
            "start_date": "2030-09-01",
            "duration_days": 7,
            "daily_minutes": 30,
        },
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session"]["id"]
    for _ in range(5):
        current = client.get(f"/api/diagnosis/sessions/{session_id}").json()
        question = current.get("current_question")
        if question is None:
            break
        answered = client.post(
            f"/api/diagnosis/sessions/{session_id}/answers",
            json={
                "idempotency_key": f"calendar-diag-answer-{time.time_ns()}",
                "question_id": question["id"],
                "selected_index": None,
                "status": "skipped",
            },
        )
        assert answered.status_code == 200, answered.text

    preview = client.post(
        f"/api/diagnosis/sessions/{session_id}/finish",
        json={
            "goal": "用一周时间补薄弱点",
            "question_count": 3,
            "start_date": "2030-09-01",
            "duration_days": 7,
            "daily_minutes": 30,
        },
    )
    assert preview.status_code == 200, preview.text
    schedule_preview = preview.json()["schedule_preview"]
    assert schedule_preview["start_date"] == "2030-09-01"
    assert schedule_preview["target_date"] == "2030-09-07"
    version_id = preview.json()["version"]["id"]
    saved_session = client.get(f"/api/diagnosis/sessions/{session_id}").json()
    assert saved_session["schedule_options"] == {
        "start_date": "2030-09-01",
        "duration_days": 7,
        "target_date": "2030-09-07",
        "daily_minutes": 30,
    }

    confirmed = client.post(
        f"/api/diagnosis/sessions/{session_id}/confirm",
        json={"idempotency_key": f"calendar-diag-confirm-{time.time_ns()}"},
    )
    assert confirmed.status_code == 200, confirmed.text

    with SessionLocal() as db:
        version = db.get(LearningPlanVersion, version_id)
        tasks = db.execute(
            select(ScheduleTask)
            .where(ScheduleTask.plan_version_id == version_id)
            .order_by(ScheduleTask.ordinal)
        ).scalars().all()
        plan_id = version.plan_id
        stored_schedule = json.loads(version.schedule_json or "{}")
    assert tasks
    assert stored_schedule["target_date"] == "2030-09-07"
    task_ids = [task.id for task in tasks]

    calendar = client.get(f"/api/schedule?plan_id={plan_id}&date=2030-09-01")
    assert calendar.status_code == 200, calendar.text
    assert calendar.json()["plan"]["unassigned"] is False
    assert calendar.json()["selected_tasks"]

    repeated = client.post(
        f"/api/diagnosis/sessions/{session_id}/confirm",
        json={
            "idempotency_key": f"calendar-diag-confirm-again-{time.time_ns()}",
            "start_date": "2030-10-01",
            "duration_days": 14,
            "daily_minutes": 60,
        },
    )
    assert repeated.status_code == 200, repeated.text
    with SessionLocal() as db:
        again = db.execute(
            select(ScheduleTask)
            .where(ScheduleTask.plan_version_id == version_id)
            .order_by(ScheduleTask.ordinal)
        ).scalars().all()
    assert [task.id for task in again] == task_ids
    assert [task.scheduled_date for task in again] == [
        task.scheduled_date for task in tasks
    ]

def test_task_links_target_reading_page_review_recap_and_practice_group(client, tmp_path):
    plan_id, version_id, pack_id, _questions = _create_plan_pack(
        client, tmp_path, "任务链接计划", module_count=1
    )
    with SessionLocal() as db:
        db.add(
            HandoutSection(
                id="sec_link_page_a",
                pack_id=pack_id,
                section_type="module_page",
                ordinal=1,
                heading="机会成本机制",
                body_text="",
                content_json=json.dumps(
                    {
                        "page_id": "opportunity-cost-mechanism",
                        "knowledge_point_ids": ["kp_cal_a1"],
                    },
                    ensure_ascii=False,
                ),
                section_role="neutral",
                is_extension=False,
                word_count=0,
                module_id="mod_cal_a",
            )
        )
        db.commit()
    _schedule_plan(client, plan_id, version_id, start="2030-08-01")
    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(ScheduleTask)
                .where(ScheduleTask.plan_version_id == version_id)
                .order_by(ScheduleTask.ordinal)
            ).scalars().all()
        )
    items = {}
    for task in rows:
        payload = client.get(
            f"/api/schedule?plan_id={plan_id}&date={task.scheduled_date}"
        ).json()
        item = next(
            entry for entry in payload["selected_tasks"] if entry["id"] == task.id
        )
        items.setdefault(task.task_type, item)

    assert "page_id=opportunity-cost-mechanism" in items["reading"]["action_url"]
    assert items["review"]["action_url"].endswith(
        f"page_id=recap-{items['review']['module_id']}"
    )
    assert "task_id=" in items["practice"]["action_url"]
    assert "page_id=" not in items["practice"]["action_url"]

    review_url = items["review"]["action_url"]
    completed = client.post(f"/api/schedule/tasks/{items['review']['id']}/complete")
    assert completed.status_code == 200, completed.text
    payload = client.get(
        f"/api/schedule?plan_id={plan_id}&date={items['review']['scheduled_date']}"
    ).json()
    again = next(
        entry for entry in payload["selected_tasks"] if entry["id"] == items["review"]["id"]
    )
    assert again["status"] == "completed"
    assert again["action_url"] == review_url


def test_next_task_and_brief_share_recap_link_for_review(client, tmp_path):
    plan_id, version_id, pack_id, _questions = _create_plan_pack(
        client, tmp_path, "下一项回顾链接计划", module_count=1
    )
    today = date.today().isoformat()
    _schedule_plan(client, plan_id, version_id, start=today)
    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(ScheduleTask)
                .where(ScheduleTask.plan_version_id == version_id)
                .order_by(ScheduleTask.ordinal)
            ).scalars().all()
        )
    reading = next(task for task in rows if task.task_type == "reading")
    practice = next(task for task in rows if task.task_type == "practice")
    assert client.post(f"/api/schedule/tasks/{reading.id}/complete").status_code == 200
    page = client.get(f"/api/packs/{pack_id}?task_id={practice.id}").json()
    answers = [
        {"question_id": item["id"], "selected_index": 0} for item in page["questions"]
    ]
    submitted = client.post(
        f"/api/packs/{pack_id}/submit",
        json={
            "idempotency_key": f"next-review-{time.time_ns()}",
            "task_id": practice.id,
            "answers": answers,
        },
    )
    assert submitted.status_code == 200, submitted.text

    calendar = client.get(f"/api/schedule?plan_id={plan_id}&date={today}").json()
    assert calendar["today_all_done"] is True
    assert calendar["next_task"]["task_type"] == "review"
    expected = f"page_id=recap-{calendar['next_task']['module_id']}"
    assert expected in calendar["next_task"]["action_url"]
    assert calendar["brief"]["next_task"]["action_url"] == calendar["next_task"]["action_url"]
