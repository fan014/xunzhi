from __future__ import annotations

import json
from datetime import date

import pytest

from app.domain import schedule
from app.domain.models import LearningPlanVersion


def _version(module_count: int = 5, question_count: int = 4) -> LearningPlanVersion:
    modules = []
    for index in range(1, module_count + 1):
        modules.append(
            {
                "module_id": f"mod_{index}",
                "title": f"模块 {index}",
                "knowledge_point_ids": [f"kp_{index}_a", f"kp_{index}_b", f"kp_{index}_c"],
                "core_points": [f"概念 {index}A", f"概念 {index}B", f"概念 {index}C"],
                "suggested_question_count": question_count,
            }
        )
    return LearningPlanVersion(
        id="pv_schedule_unit",
        plan_id="plan_schedule_unit",
        status="confirmed",
        sources_json="[]",
        scope_json="{}",
        items_json="[]",
        adjustment_json="[]",
        modules_json=json.dumps(modules, ensure_ascii=False),
        question_count=module_count * question_count,
        created_at="2026-11-24T00:00:00Z",
    )


def test_tasks_follow_module_order_and_keep_stable_ordinals():
    version = _version(module_count=2, question_count=4)
    tasks = schedule.build_tasks(version, daily_minutes=30)
    assert [task["task_type"] for task in tasks] == [
        "reading",
        "reading",
        "practice",
        "review",
        "reading",
        "reading",
        "practice",
        "review",
    ]
    assert [task["ordinal"] for task in tasks] == list(range(1, len(tasks) + 1))
    assert tasks[0]["module_id"] == "mod_1"
    assert tasks[0]["knowledge_point_ids"] == ["kp_1_a", "kp_1_b"]
    assert tasks[1]["knowledge_point_ids"] == ["kp_1_c"]
    assert "概念 1A" in tasks[0]["task_title"]
    assert tasks[2]["estimated_minutes"] == 20


def test_preview_changes_target_and_reports_capacity_gap():
    version = _version(module_count=5, question_count=4)
    week = schedule.preview_schedule(
        version,
        {"start_date": "2030-01-01", "duration_days": 7, "daily_minutes": 30},
        today=date(2030, 1, 1),
    )
    fortnight = schedule.preview_schedule(
        version,
        {"start_date": "2030-01-01", "duration_days": 14, "daily_minutes": 30},
        today=date(2030, 1, 1),
    )
    assert week["task_count"] == fortnight["task_count"] == 20
    assert week["target_date"] == "2030-01-07"
    assert fortnight["target_date"] == "2030-01-14"
    assert week["over_capacity"] is True
    assert week["shortfall_minutes"] > 0
    assert fortnight["over_capacity"] is False
    assert fortnight["shortfall_minutes"] == 0
    assert week["planned_end_date"] == fortnight["planned_end_date"]
    assert week["tasks"][0]["scheduled_date"] == "2030-01-01"


def test_preview_crosses_month_and_states_use_local_date():
    version = _version(module_count=2, question_count=4)
    preview = schedule.preview_schedule(
        version,
        {"start_date": "2030-01-28", "duration_days": 14, "daily_minutes": 15},
        today=date(2030, 1, 28),
    )
    scheduled_dates = [task["scheduled_date"] for task in preview["tasks"]]
    assert scheduled_dates == sorted(scheduled_dates)
    assert scheduled_dates[0] == "2030-01-28"
    assert any(item.startswith("2030-02-") for item in scheduled_dates)
    assert preview["target_date"] == "2030-02-10"
    assert schedule.task_state(
        {"status": "pending", "scheduled_date": "2030-01-27"}, today=date(2030, 1, 28)
    ) == "overdue"
    assert schedule.task_state(
        {"status": "pending", "scheduled_date": "2030-01-28"}, today=date(2030, 1, 28)
    ) == "today"
    assert schedule.task_state(
        {"status": "completed", "scheduled_date": "2030-01-01"}, today=date(2030, 1, 28)
    ) == "completed"


def test_custom_target_and_invalid_options():
    version = _version(module_count=1, question_count=2)
    preview = schedule.preview_schedule(
        version,
        {"start_date": "2030-03-01", "target_date": "2030-03-10", "daily_minutes": 30},
        today=date(2030, 3, 1),
    )
    assert preview["duration_days"] == 10
    assert preview["target_date"] == "2030-03-10"
    with pytest.raises(schedule.ScheduleError) as error:
        schedule.preview_schedule(
            version,
            {"start_date": "2030-03-10", "target_date": "2030-03-01"},
            today=date(2030, 3, 1),
        )
    assert error.value.code == "schedule_target_before_start"
    with pytest.raises(schedule.ScheduleError) as error:
        schedule.preview_schedule(
            version, {"start_date": "2030-03-01", "daily_minutes": 0}, today=date(2030, 3, 1)
        )
    assert error.value.code == "schedule_daily_invalid"
    with pytest.raises(schedule.ScheduleError) as error:
        schedule.preview_schedule(
            version, {"start_date": "not-a-date"}, today=date(2030, 3, 1)
        )
    assert error.value.code == "schedule_date_invalid"