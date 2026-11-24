from __future__ import annotations

from datetime import date

from app.domain.schedule import compute_shift, completed_ahead_dates


def task(task_id, scheduled, status="pending", minutes=10, completed_at=None, task_type="reading"):
    return {
        "id": task_id,
        "task_title": f"任务 {task_id}",
        "module_id": "mod",
        "task_type": task_type,
        "status": status,
        "scheduled_date": scheduled,
        "estimated_minutes": minutes,
        "completed_at": completed_at,
    }


def test_completed_ahead_dates_only_counts_contiguous_full_days():
    today = date(2026, 11, 1)
    tasks = [
        task("d1", "2026-11-01", "completed", completed_at="2026-11-01T09:00:00"),
        task("d2a", "2026-11-02", "completed", completed_at="2026-11-01T10:00:00"),
        task("d2b", "2026-11-02", "completed", completed_at="2026-11-01T10:30:00"),
        task("d3", "2026-11-03", "pending"),
        task("d4", "2026-11-04", "pending"),
    ]
    assert completed_ahead_dates(tasks, today) == ["2026-11-02"]
    shift = compute_shift(tasks, today, "pv")
    assert shift["available"] is True
    assert shift["shift_days"] == 1
    assert [(item["task_id"], item["from_date"], item["to_date"]) for item in shift["changes"]] == [
        ("d3", "2026-11-03", "2026-11-02"),
        ("d4", "2026-11-04", "2026-11-03"),
    ]
    assert shift["planned_end_date"] == "2026-11-03"
    assert shift["preview_key"]


def test_partial_ahead_day_blocks_shift():
    today = date(2026, 11, 1)
    tasks = [
        task("d2a", "2026-11-02", "completed", completed_at="2026-11-01T10:00:00"),
        task("d2b", "2026-11-02", "pending"),
        task("d3", "2026-11-03", "pending"),
    ]
    assert completed_ahead_dates(tasks, today) == []
    assert compute_shift(tasks, today, "pv")["available"] is False


def test_blank_day_stops_consecutive_prefix_but_still_shifts():
    today = date(2026, 11, 1)
    tasks = [
        task("d2a", "2026-11-02", "completed", completed_at="2026-11-01T10:00:00"),
        task("d4", "2026-11-04", "pending"),
        task("d5", "2026-11-05", "pending"),
    ]
    assert completed_ahead_dates(tasks, today) == ["2026-11-02"]
    shift = compute_shift(tasks, today, "pv")
    assert shift["available"] is True
    assert [
        (item["from_date"], item["to_date"]) for item in shift["changes"]
    ] == [("2026-11-04", "2026-11-03"), ("2026-11-05", "2026-11-04")]
    assert shift["days"][0]["date"] == "2026-11-03"
    assert shift["days"][0]["task_count"] == 1
    assert shift["days"][0]["estimated_minutes"] == 10


def test_completed_tasks_keep_dates_and_no_pending_means_no_prompt():
    today = date(2026, 11, 1)
    tasks = [
        task("d2", "2026-11-02", "completed", completed_at="2026-11-01T10:00:00"),
        task("d1", "2026-11-01", "completed", completed_at="2026-11-01T09:00:00"),
    ]
    assert compute_shift(tasks, today, "pv")["available"] is False


def test_shift_never_moves_into_today_or_past():
    today = date(2026, 11, 1)
    tasks = [
        task("late", "2026-10-30", "pending"),
        task("d2", "2026-11-02", "completed", completed_at="2026-11-01T10:00:00"),
        task("d3", "2026-11-03", "pending"),
    ]
    shift = compute_shift(tasks, today, "pv")
    assert shift["available"] is True
    assert [(item["from_date"], item["to_date"]) for item in shift["changes"]] == [
        ("2026-11-03", "2026-11-02")
    ]
    assert shift["planned_end_date"] == "2026-11-02"