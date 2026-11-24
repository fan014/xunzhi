from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import (
    Attempt,
    AttemptAnswer,
    LearningPack,
    LearningPlan,
    LearningPlanVersion,
    PackPlanVersion,
    Question,
    ScheduleTask,
    now_iso,
)
from app.infra.ids import new_id

DEFAULT_DURATION_DAYS = 14
DEFAULT_DAILY_MINUTES = 30
MIN_DAILY_MINUTES = 5
MAX_DAILY_MINUTES = 24 * 60
MAX_DURATION_DAYS = 3650
READING_MINUTES_PER_POINT = 8
READING_MIN_MINUTES = 6
PRACTICE_MINUTES_PER_QUESTION = 5
PRACTICE_MIN_MINUTES = 10
REVIEW_MINUTES = 5
PRACTICE_QUESTIONS_PER_TASK = 4
READING_POINTS_PER_TASK = 2

TASK_TYPES = {"reading", "practice", "review"}
MANUAL_TASK_TYPES = {"reading", "review"}


class ScheduleError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def today_local() -> date:
    return datetime.now().date()


def local_now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def parse_iso_date(value, field: str = "date") -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ScheduleError(
            "schedule_date_invalid", f"{field} 需要 YYYY-MM-DD 格式的日期", 422
        ) from exc


def normalize_options(options: dict | None, today: date | None = None) -> dict:
    today = today or today_local()
    raw = options or {}
    start = parse_iso_date(raw.get("start_date"), "start_date") or today
    target = parse_iso_date(raw.get("target_date"), "target_date")
    if target is not None:
        if target < start:
            raise ScheduleError(
                "schedule_target_before_start", "预计完成日期不能早于开始日期", 422
            )
        duration = (target - start).days + 1
    else:
        value = raw.get("duration_days")
        try:
            duration = int(value) if value not in (None, "") else DEFAULT_DURATION_DAYS
        except (TypeError, ValueError) as exc:
            raise ScheduleError(
                "schedule_duration_invalid", "学习周期需要填写天数", 422
            ) from exc
    if duration < 1 or duration > MAX_DURATION_DAYS:
        raise ScheduleError(
            "schedule_duration_invalid",
            f"学习周期需要在 1–{MAX_DURATION_DAYS} 天之间",
            422,
        )
    value = raw.get("daily_minutes")
    try:
        daily = int(value) if value not in (None, "") else DEFAULT_DAILY_MINUTES
    except (TypeError, ValueError) as exc:
        raise ScheduleError(
            "schedule_daily_invalid", "每天学习时间需要填写分钟数", 422
        ) from exc
    if daily < MIN_DAILY_MINUTES or daily > MAX_DAILY_MINUTES:
        raise ScheduleError(
            "schedule_daily_invalid",
            f"每天学习时间需要在 {MIN_DAILY_MINUTES}–{MAX_DAILY_MINUTES} 分钟之间",
            422,
        )
    return {
        "start_date": start,
        "target_date": start + timedelta(days=duration - 1),
        "duration_days": duration,
        "daily_minutes": daily,
    }


def _chunks(values: list, size: int) -> list[list]:
    if size < 1:
        size = 1
    return [values[index : index + size] for index in range(0, len(values), size)]


def _module_list(version: LearningPlanVersion) -> list[dict]:
    try:
        modules = json.loads(version.modules_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return modules if isinstance(modules, list) else []


def build_tasks(version: LearningPlanVersion, daily_minutes: int = DEFAULT_DAILY_MINUTES) -> list[dict]:
    modules = _module_list(version)
    tasks: list[dict] = []
    if modules:
        points_per_task = max(
            1, min(READING_POINTS_PER_TASK, daily_minutes // READING_MINUTES_PER_POINT)
        )
        questions_per_task = max(
            1,
            min(
                PRACTICE_QUESTIONS_PER_TASK,
                daily_minutes // PRACTICE_MINUTES_PER_QUESTION,
            ),
        )
        for module_index, module in enumerate(modules, start=1):
            module_id = str(module.get("module_id") or f"module_{module_index}")
            module_title = str(module.get("title") or f"模块 {module_index}").strip()
            point_ids = [str(item) for item in (module.get("knowledge_point_ids") or []) if item]
            point_names = [str(item) for item in (module.get("core_points") or []) if item]
            if point_ids:
                chunks = _chunks(point_ids, points_per_task)
                for chunk_index, chunk in enumerate(chunks, start=1):
                    names = [
                        point_names[index]
                        if index < len(point_names) and point_names[index]
                        else module_title
                        for index, _ in enumerate(chunk)
                    ]
                    names = list(dict.fromkeys(names))
                    suffix = f"（{chunk_index}/{len(chunks)}）" if len(chunks) > 1 else ""
                    tasks.append(
                        {
                            "module_id": module_id,
                            "module_title": module_title,
                            "task_type": "reading",
                            "task_title": f"阅读：{'、'.join(names)}{suffix}",
                            "knowledge_point_ids": chunk,
                            "estimated_minutes": max(
                                READING_MIN_MINUTES,
                                READING_MINUTES_PER_POINT * len(chunk),
                            ),
                        }
                    )
            else:
                tasks.append(
                    {
                        "module_id": module_id,
                        "module_title": module_title,
                        "task_type": "reading",
                        "task_title": f"阅读：{module_title}",
                        "knowledge_point_ids": [],
                        "estimated_minutes": max(READING_MIN_MINUTES, READING_MINUTES_PER_POINT),
                    }
                )
            question_count = int(module.get("suggested_question_count") or 0)
            if question_count > 0:
                groups = _chunks(list(range(question_count)), questions_per_task)
                for group_index, group in enumerate(groups, start=1):
                    count = len(group)
                    suffix = f" · 第 {group_index} 组" if len(groups) > 1 else ""
                    tasks.append(
                        {
                            "module_id": module_id,
                            "module_title": module_title,
                            "task_type": "practice",
                            "task_title": f"练习：{module_title} · {count} 道{suffix}",
                            "knowledge_point_ids": point_ids,
                            "planned_question_count": count,
                            "estimated_minutes": max(
                                PRACTICE_MIN_MINUTES,
                                PRACTICE_MINUTES_PER_QUESTION * count,
                            ),
                        }
                    )
            tasks.append(
                {
                    "module_id": module_id,
                    "module_title": module_title,
                    "task_type": "review",
                    "task_title": f"回顾：{module_title}",
                    "knowledge_point_ids": point_ids,
                    "estimated_minutes": REVIEW_MINUTES,
                }
            )
    else:
        question_count = int(version.question_count or 0)
        module_id = "review"
        module_title = "本次学习内容"
        tasks.append(
            {
                "module_id": module_id,
                "module_title": module_title,
                "task_type": "reading",
                "task_title": "阅读：本次学习内容",
                "knowledge_point_ids": [],
                "estimated_minutes": max(READING_MIN_MINUTES, READING_MINUTES_PER_POINT * 2),
            }
        )
        if question_count > 0:
            questions_per_task = max(
                1,
                min(
                    PRACTICE_QUESTIONS_PER_TASK,
                    daily_minutes // PRACTICE_MINUTES_PER_QUESTION,
                ),
            )
            groups = _chunks(list(range(question_count)), questions_per_task)
            for group_index, group in enumerate(groups, start=1):
                count = len(group)
                suffix = f" · 第 {group_index} 组" if len(groups) > 1 else ""
                tasks.append(
                    {
                        "module_id": module_id,
                        "module_title": module_title,
                        "task_type": "practice",
                        "task_title": f"练习：本次学习内容 · {count} 道{suffix}",
                        "knowledge_point_ids": [],
                        "planned_question_count": count,
                        "estimated_minutes": max(
                            PRACTICE_MIN_MINUTES,
                            PRACTICE_MINUTES_PER_QUESTION * count,
                        ),
                    }
                )
        tasks.append(
            {
                "module_id": module_id,
                "module_title": module_title,
                "task_type": "review",
                "task_title": "回顾：本次学习内容",
                "knowledge_point_ids": [],
                "estimated_minutes": REVIEW_MINUTES,
            }
        )
    for ordinal, task in enumerate(tasks, start=1):
        task["ordinal"] = ordinal
    return tasks


def _assign_dates(tasks: list[dict], start: date, daily_minutes: int) -> list[dict]:
    assigned: list[dict] = []
    day_offset = 0
    used = 0
    for task in tasks:
        minutes = max(1, int(task.get("estimated_minutes") or 1))
        if used > 0 and used + minutes > daily_minutes:
            day_offset += 1
            used = 0
        scheduled = start + timedelta(days=day_offset)
        assigned.append({**task, "scheduled_date": scheduled.isoformat()})
        used += minutes
        if used >= daily_minutes:
            day_offset += 1
            used = 0
    return assigned


def _summarize(tasks: list[dict], start: date, target: date, duration: int, daily: int) -> dict:
    total = sum(int(task.get("estimated_minutes") or 0) for task in tasks)
    capacity = duration * daily
    planned_end = parse_iso_date(tasks[-1]["scheduled_date"]) if tasks else None
    breakdown = {"reading": 0, "practice": 0, "review": 0}
    for task in tasks:
        kind = task.get("task_type") or "reading"
        breakdown[kind] = breakdown.get(kind, 0) + int(task.get("estimated_minutes") or 0)
    shortfall = max(0, total - capacity)
    over_capacity = bool(shortfall > 0 or (planned_end is not None and planned_end > target))
    return {
        "start_date": start.isoformat(),
        "target_date": target.isoformat(),
        "duration_days": duration,
        "daily_minutes": daily,
        "task_count": len(tasks),
        "estimated_minutes": total,
        "capacity_minutes": capacity,
        "shortfall_minutes": shortfall,
        "over_capacity": over_capacity,
        "planned_end_date": planned_end.isoformat() if planned_end is not None else None,
        "breakdown": breakdown,
    }


def task_state(task: ScheduleTask | dict, today: date | None = None) -> str:
    today = today or today_local()
    status = task.get("status") if isinstance(task, dict) else task.status
    scheduled = task.get("scheduled_date") if isinstance(task, dict) else task.scheduled_date
    if status == "completed":
        return "completed"
    if scheduled and scheduled < today.isoformat():
        return "overdue"
    if scheduled == today.isoformat():
        return "today"
    return "upcoming"


def _task_question_ids(task: ScheduleTask | dict) -> list[str]:
    raw = task.get("question_ids") if isinstance(task, dict) else task.question_ids_json
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(values, list):
        return []
    return [str(item) for item in values if item]


def _task_payload(
    task: ScheduleTask | dict,
    today: date,
    *,
    pack: LearningPack | None = None,
    preview: bool = False,
) -> dict:
    if isinstance(task, dict):
        planned = int(task.get("planned_question_count") or 0)
        question_ids = _task_question_ids(task)
        row = {
            "id": task.get("id"),
            "module_id": task.get("module_id"),
            "module_title": task.get("module_title"),
            "task_title": task.get("task_title"),
            "task_type": task.get("task_type"),
            "ordinal": task.get("ordinal"),
            "scheduled_date": task.get("scheduled_date"),
            "estimated_minutes": task.get("estimated_minutes"),
            "planned_question_count": planned,
            "question_pack_id": task.get("question_pack_id"),
            "knowledge_point_ids": task.get("knowledge_point_ids") or [],
            "status": task.get("status") or "pending",
            "completed_at": task.get("completed_at"),
            "completion_source": task.get("completion_source"),
        }
    else:
        try:
            point_ids = json.loads(task.knowledge_point_ids_json or "[]")
        except (TypeError, json.JSONDecodeError):
            point_ids = []
        planned = int(task.planned_question_count or 0)
        question_ids = _task_question_ids(task)
        row = {
            "id": task.id,
            "module_id": task.module_id,
            "module_title": task.module_title,
            "task_title": task.task_title,
            "task_type": task.task_type,
            "ordinal": task.ordinal,
            "scheduled_date": task.scheduled_date,
            "estimated_minutes": task.estimated_minutes,
            "planned_question_count": planned,
            "question_pack_id": task.question_pack_id,
            "knowledge_point_ids": point_ids if isinstance(point_ids, list) else [],
            "status": task.status,
            "completed_at": task.completed_at,
            "completion_source": task.completion_source,
        }
    row["state"] = task_state(row, today)
    row["can_mark_complete"] = row["status"] == "pending" and row["task_type"] in MANUAL_TASK_TYPES
    row["preview"] = preview
    row["completed_ahead"] = bool(
        row["status"] == "completed"
        and row["completed_at"]
        and str(row["completed_at"])[:10] < str(row["scheduled_date"])
    )
    if row["task_type"] == "practice":
        row["practice_question_count"] = len(question_ids) if question_ids else planned
        row["practice_available"] = bool(pack is not None and question_ids)
        if pack is None:
            row["practice_unavailable_reason"] = "学习内容尚未生成"
        elif not question_ids:
            row["practice_unavailable_reason"] = "本组在本次学习内容中没有可对应题目"
        else:
            row["practice_unavailable_reason"] = None
    else:
        row["practice_question_count"] = 0
        row["practice_available"] = True
        row["practice_unavailable_reason"] = None
    if pack is not None and not preview:
        if row["task_type"] == "practice":
            if question_ids:
                row["action_url"] = f"/packs/{pack.id}?task_id={row['id']}&view=questions"
            else:
                row["action_url"] = None
        else:
            row["action_url"] = f"/packs/{pack.id}?module_id={row['module_id']}&view=modules"
    else:
        row["action_url"] = None
    return row


def serialize_task(task: ScheduleTask, today: date | None = None, pack: LearningPack | None = None) -> dict:
    return _task_payload(task, today or today_local(), pack=pack)


def _days(tasks: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for task in tasks:
        day = grouped.setdefault(
            task["scheduled_date"],
            {
                "date": task["scheduled_date"],
                "tasks": [],
                "estimated_minutes": 0,
                "total": 0,
                "completed": 0,
            },
        )
        day["tasks"].append(task)
        day["estimated_minutes"] += int(task.get("estimated_minutes") or 0)
        day["total"] += 1
        if task.get("status") == "completed":
            day["completed"] += 1
    return list(grouped.values())


def preview_schedule(
    version: LearningPlanVersion, options: dict | None = None, today: date | None = None
) -> dict:
    today = today or today_local()
    normalized = normalize_options(options, today)
    built = build_tasks(version, normalized["daily_minutes"])
    assigned = _assign_dates(built, normalized["start_date"], normalized["daily_minutes"])
    summary = _summarize(
        assigned,
        normalized["start_date"],
        normalized["target_date"],
        normalized["duration_days"],
        normalized["daily_minutes"],
    )
    payload_tasks = [
        _task_payload(
            {
                **task,
                "id": f"preview:{version.id}:{task['ordinal']}",
                "status": "pending",
                "completed_at": None,
                "completion_source": None,
            },
            today,
            preview=True,
        )
        for task in assigned
    ]
    return {
        "plan_id": version.plan_id,
        "version_id": version.id,
        "preview": True,
        "unassigned": False,
        **summary,
        "tasks": payload_tasks,
        "days": _days(payload_tasks),
    }


def tasks_for_version(db: Session, version_id: str) -> list[ScheduleTask]:
    return list(
        db.execute(
            select(ScheduleTask)
            .where(ScheduleTask.plan_version_id == version_id)
            .order_by(ScheduleTask.ordinal)
        ).scalars().all()
    )


def schedule_config(version: LearningPlanVersion) -> dict:
    try:
        value = json.loads(version.schedule_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def progress_payload(tasks: list[ScheduleTask], today: date | None = None) -> dict:
    today = today or today_local()
    total = len(tasks)
    completed = sum(1 for task in tasks if task.status == "completed")
    overdue = sum(
        1 for task in tasks if task.status != "completed" and task.scheduled_date < today.isoformat()
    )
    today_tasks = [task for task in tasks if task.scheduled_date == today.isoformat()]
    today_completed = sum(1 for task in today_tasks if task.status == "completed")
    return {
        "total": total,
        "completed": completed,
        "percent": int(round(completed * 100 / total)) if total else 0,
        "label": f"已完成 {completed} / {total} 项" if total else "未安排日程",
        "today_total": len(today_tasks),
        "today_completed": today_completed,
        "overdue_count": overdue,
        "all_done": bool(total and completed == total),
    }


def latest_pack_for_version(db: Session, version_id: str) -> LearningPack | None:
    row = db.execute(
        select(LearningPack)
        .join(PackPlanVersion, PackPlanVersion.pack_id == LearningPack.id)
        .where(
            PackPlanVersion.plan_version_id == version_id,
            LearningPack.source_kind != "diagnostic",
        )
        .order_by(LearningPack.created_at.desc())
    ).scalars().first()
    return row


def pack_for_task(db: Session, task: ScheduleTask, fallback: LearningPack | None) -> LearningPack | None:
    if task.status == "completed" and task.attempt_id:
        attempt = db.get(Attempt, task.attempt_id)
        if attempt is not None:
            pack = db.get(LearningPack, attempt.pack_id)
            if pack is not None:
                return pack
    if task.status == "completed" and task.question_pack_id:
        pack = db.get(LearningPack, task.question_pack_id)
        if pack is not None:
            return pack
    return fallback


def _pack_questions(db: Session, pack_id: str) -> list[Question]:
    return list(
        db.execute(
            select(Question).where(Question.pack_id == pack_id).order_by(Question.ordinal)
        ).scalars().all()
    )


def _valid_question_ids(db: Session, pack_id: str, values: list[str]) -> bool:
    if not values:
        return False
    existing = {
        row[0]
        for row in db.execute(
            select(Question.id).where(
                Question.pack_id == pack_id, Question.id.in_(values)
            )
        ).all()
    }
    return len(existing) == len(set(values))


def bind_practice_questions(
    db: Session, pack: LearningPack, version_id: str | None = None
) -> dict[str, list[str]]:
    if version_id is None:
        adoption = db.get(PackPlanVersion, pack.id)
        if adoption is None:
            return {}
        version_id = adoption.plan_version_id
    tasks = list(
        db.execute(
            select(ScheduleTask)
            .where(
                ScheduleTask.plan_version_id == version_id,
                ScheduleTask.task_type == "practice",
            )
            .order_by(ScheduleTask.ordinal)
        ).scalars().all()
    )
    questions = _pack_questions(db, pack.id)
    by_module: dict[str, list[str]] = {}
    for question in questions:
        by_module.setdefault(question.module_id or "", []).append(question.id)
    by_task_module: dict[str, list[ScheduleTask]] = {}
    for task in tasks:
        by_task_module.setdefault(task.module_id, []).append(task)

    bindings: dict[str, list[str]] = {}
    for module_id, module_tasks in by_task_module.items():
        available = by_module.get(module_id, [])
        bindable = [
            task
            for task in module_tasks
            if not (
                task.status == "completed"
                and task.question_pack_id
                and task.question_pack_id != pack.id
            )
        ]
        if not bindable:
            for task in module_tasks:
                bindings[task.id] = _task_question_ids(task)
            continue
        if not bindable:
            for task in module_tasks:
                bindings[task.id] = _task_question_ids(task)
            continue
        available_set = set(available)
        preserved: dict[str, list[str]] = {}
        used: set[str] = set()
        for task in bindable:
            ids = _task_question_ids(task)
            if (
                task.question_pack_id == pack.id
                and ids
                and set(ids).issubset(available_set)
                and _valid_question_ids(db, pack.id, ids)
            ):
                preserved[task.id] = ids
                used.update(ids)
        unbound = [task for task in bindable if task.id not in preserved]
        remaining = [item for item in available if item not in used]
        count = len(remaining)
        group_count = len(unbound)
        base = count // group_count if group_count else 0
        remainder = count % group_count if group_count else 0
        offset = 0
        for index, task in enumerate(unbound):
            size = base + (1 if index < remainder else 0)
            ids = remaining[offset : offset + size]
            offset += size
            task.question_pack_id = pack.id
            task.question_ids_json = json.dumps(ids, ensure_ascii=False)
            if not task.planned_question_count:
                task.planned_question_count = len(ids)
            task.updated_at = now_iso()
            bindings[task.id] = ids
        bindings.update(preserved)
        for task in module_tasks:
            if task.id not in bindings:
                bindings[task.id] = _task_question_ids(task)
        for task in module_tasks:
            if task.status == "completed" and task.question_pack_id != pack.id:
                continue
            if task.question_pack_id != pack.id:
                continue
            ids = _task_question_ids(task)
            if not task.planned_question_count:
                task.planned_question_count = len(ids)
    db.flush()
    return bindings


def ensure_task_question_ids(
    db: Session, task: ScheduleTask, pack: LearningPack | None
) -> list[str]:
    if task.task_type != "practice":
        return []
    if pack is None:
        return []
    existing = _task_question_ids(task)
    if (
        task.question_pack_id == pack.id
        and existing
        and _valid_question_ids(db, pack.id, existing)
    ):
        return existing
    bindings = bind_practice_questions(db, pack, task.plan_version_id)
    return bindings.get(task.id) or []


def attempt_question_ids(db: Session, pack: LearningPack, attempt: Attempt) -> list[str]:
    if attempt.schedule_task_id:
        task = db.get(ScheduleTask, attempt.schedule_task_id)
        if task is not None:
            return ensure_task_question_ids(db, task, pack)
        return []
    return [question.id for question in _pack_questions(db, pack.id)]


def _answered_question_ids(db: Session, attempt_id: str) -> set[str]:
    return {
        row.question_id
        for row in db.execute(
            select(AttemptAnswer).where(AttemptAnswer.attempt_id == attempt_id)
        ).scalars().all()
        if row.selected_index is not None
    }


def _mark_practice_complete(task: ScheduleTask, attempt: Attempt) -> bool:
    if task.status == "completed":
        return False
    task.status = "completed"
    task.completed_at = local_now_iso()
    task.completion_source = "practice"
    task.attempt_id = attempt.id
    task.updated_at = now_iso()
    return True


def settle_practice_tasks(
    db: Session, pack: LearningPack, attempt: Attempt, task_id: str | None = None
) -> int:
    adoption = db.get(PackPlanVersion, pack.id)
    if adoption is None:
        return 0
    answered = _answered_question_ids(db, attempt.id)
    if task_id:
        task = db.get(ScheduleTask, task_id)
        if (
            task is None
            or task.task_type != "practice"
            or task.plan_version_id != adoption.plan_version_id
        ):
            return 0
        ids = ensure_task_question_ids(db, task, pack)
        if ids and set(ids).issubset(answered):
            return 1 if _mark_practice_complete(task, attempt) else 0
        return 0
    tasks = list(
        db.execute(
            select(ScheduleTask)
            .where(
                ScheduleTask.plan_version_id == adoption.plan_version_id,
                ScheduleTask.task_type == "practice",
                ScheduleTask.status == "pending",
            )
            .order_by(ScheduleTask.ordinal)
        ).scalars().all()
    )
    if not tasks:
        return 0
    bindings = bind_practice_questions(db, pack, adoption.plan_version_id)
    completed = 0
    for task in tasks:
        ids = bindings.get(task.id) or ensure_task_question_ids(db, task, pack)
        if ids and set(ids).issubset(answered):
            if _mark_practice_complete(task, attempt):
                completed += 1
    db.flush()
    return completed


def practice_scope_payload(
    db: Session,
    pack: LearningPack,
    task: ScheduleTask,
    today: date | None = None,
) -> dict:
    today = today or today_local()
    ids = ensure_task_question_ids(db, task, pack)
    submitted = None
    if task.attempt_id:
        candidate = db.get(Attempt, task.attempt_id)
        if (
            candidate is not None
            and candidate.pack_id == pack.id
            and candidate.status == "submitted"
        ):
            submitted = candidate
    if submitted is None:
        submitted = db.execute(
            select(Attempt)
            .where(
                Attempt.pack_id == pack.id,
                Attempt.schedule_task_id == task.id,
                Attempt.status == "submitted",
            )
            .order_by(Attempt.submitted_at.desc(), Attempt.id.desc())
        ).scalars().first()
    answered = _answered_question_ids(db, submitted.id) if submitted is not None else set()
    total = len(ids)
    completed = task.status == "completed"
    remaining = 0 if completed else max(0, total - len(answered.intersection(ids)))
    ahead = bool(
        completed
        and task.completed_at
        and str(task.completed_at)[:10] < task.scheduled_date
    )
    reason = None
    if not ids:
        reason = "本组在本次学习内容中没有可对应题目，请重新生成学习内容或查看整套练习。"
    return {
        "task_id": task.id,
        "task_title": task.task_title,
        "module_id": task.module_id,
        "module_title": task.module_title,
        "scheduled_date": task.scheduled_date,
        "total": total,
        "answered_count": len(answered.intersection(ids)),
        "remaining": remaining,
        "completed": completed,
        "completed_at": task.completed_at,
        "completed_ahead": ahead,
        "available": bool(ids),
        "reason": reason,
        "question_ids": ids,
        "last_attempt_id": submitted.id if submitted is not None else None,
        "last_submitted_at": submitted.submitted_at if submitted is not None else None,
        "planned_question_count": task.planned_question_count,
        "state": task_state(task, today),
    }


def active_version(db: Session, plan: LearningPlan) -> LearningPlanVersion | None:
    if plan.current_version_id:
        current = db.get(LearningPlanVersion, plan.current_version_id)
        if current is not None and current.status == "confirmed":
            return current
    rows = db.execute(
        select(LearningPlanVersion)
        .where(
            LearningPlanVersion.plan_id == plan.id,
            LearningPlanVersion.status == "confirmed",
        )
        .order_by(LearningPlanVersion.confirmed_at.desc(), LearningPlanVersion.created_at.desc())
    ).scalars().all()
    return rows[0] if rows else None


def ensure_schedule(
    db: Session,
    plan: LearningPlan,
    version: LearningPlanVersion,
    options: dict | None = None,
    *,
    today: date | None = None,
) -> tuple[dict, bool]:
    today = today or today_local()
    existing = tasks_for_version(db, version.id)
    if existing:
        config = schedule_config(version)
        payload = {
            "plan_id": plan.id,
            "version_id": version.id,
            "unassigned": False,
            "duplicate": True,
            **config,
            "progress": progress_payload(existing, today),
            "tasks": [
                _task_payload(task, today, pack=latest_pack_for_version(db, version.id))
                for task in existing
            ],
        }
        return payload, True
    preview = preview_schedule(version, options, today)
    created_at = now_iso()
    rows: list[ScheduleTask] = []
    for item in preview["tasks"]:
        row = ScheduleTask(
            id=new_id("schedule_task"),
            plan_id=plan.id,
            plan_version_id=version.id,
            module_id=item["module_id"],
            module_title=item["module_title"],
            task_title=item["task_title"],
            task_type=item["task_type"],
            ordinal=item["ordinal"],
            scheduled_date=item["scheduled_date"],
            estimated_minutes=item["estimated_minutes"],
            planned_question_count=int(item.get("planned_question_count") or 0),
            knowledge_point_ids_json=json.dumps(item["knowledge_point_ids"], ensure_ascii=False),
            status="pending",
            created_at=created_at,
            updated_at=created_at,
        )
        db.add(row)
        rows.append(row)
    config = {
        key: preview[key]
        for key in (
            "start_date",
            "target_date",
            "duration_days",
            "daily_minutes",
            "task_count",
            "estimated_minutes",
            "capacity_minutes",
            "shortfall_minutes",
            "over_capacity",
            "planned_end_date",
            "breakdown",
        )
    }
    config["saved_at"] = created_at
    version.schedule_json = json.dumps(config, ensure_ascii=False)
    plan.updated_at = now_iso()
    db.flush()
    payload = {
        "plan_id": plan.id,
        "version_id": version.id,
        "unassigned": False,
        "duplicate": False,
        **config,
        "progress": progress_payload(rows, today),
        "tasks": [
            _task_payload(task, today, pack=latest_pack_for_version(db, version.id))
            for task in rows
        ],
    }
    return payload, False


def mark_task_complete(db: Session, task: ScheduleTask, source: str = "manual") -> dict:
    if task.task_type not in MANUAL_TASK_TYPES:
        raise ScheduleError(
            "practice_requires_submission", "练习任务以实际交卷为完成依据", 409
        )
    if task.status == "completed":
        return {"task": _task_payload(task, today_local()), "duplicate": True}
    completed_at = local_now_iso()
    task.status = "completed"
    task.completed_at = completed_at
    task.completion_source = source
    task.updated_at = now_iso()
    db.flush()
    return {"task": _task_payload(task, today_local()), "duplicate": False}


def complete_practice_tasks(db: Session, pack: LearningPack, attempt_id: str) -> int:
    attempt = db.get(Attempt, attempt_id)
    if attempt is None:
        return 0
    return settle_practice_tasks(db, pack, attempt)


def update_task_dates(db: Session, version: LearningPlanVersion, changes: dict[str, str]) -> dict:
    tasks = tasks_for_version(db, version.id)
    if not tasks:
        raise ScheduleError("schedule_not_found", "该学习计划还没有安排日程", 404)
    by_id = {task.id: task for task in tasks}
    for task_id, value in changes.items():
        task = by_id.get(task_id)
        if task is None:
            raise ScheduleError("schedule_task_not_found", "日程任务不存在", 404)
        if task.status == "completed":
            continue
        parsed = parse_iso_date(value, "scheduled_date")
        if parsed is None:
            raise ScheduleError("schedule_date_invalid", "日期不能为空", 422)
        task.scheduled_date = parsed.isoformat()
        task.updated_at = now_iso()
    db.flush()
    config = schedule_config(version)
    if config:
        planned_end = max(task.scheduled_date for task in tasks)
        config["planned_end_date"] = planned_end
        config["over_capacity"] = bool(
            config.get("shortfall_minutes", 0) > 0 or planned_end > config.get("target_date", planned_end)
        )
        version.schedule_json = json.dumps(config, ensure_ascii=False)
    return {
        "version_id": version.id,
        "tasks": [_task_payload(task, today_local()) for task in tasks],
    }

SHIFT_DISMISSALS_KEY = "shift_dismissals"
LAST_SHIFT_KEY = "last_shift_key"


def _shift_view(task: ScheduleTask | dict) -> dict:
    if isinstance(task, dict):
        return {
            "id": task.get("id"),
            "task_title": task.get("task_title") or task.get("module_title") or "",
            "task_type": task.get("task_type") or "reading",
            "module_id": task.get("module_id"),
            "status": task.get("status") or "pending",
            "scheduled_date": task["scheduled_date"],
            "estimated_minutes": int(task.get("estimated_minutes") or 0),
            "completed_at": task.get("completed_at"),
        }
    return {
        "id": task.id,
        "task_title": task.task_title or task.module_title or "",
        "task_type": task.task_type,
        "module_id": task.module_id,
        "status": task.status,
        "scheduled_date": task.scheduled_date,
        "estimated_minutes": int(task.estimated_minutes or 0),
        "completed_at": task.completed_at,
    }


def completed_ahead_dates(tasks: list[ScheduleTask | dict], today: date | None = None) -> list[str]:
    today = today or today_local()
    by_date: dict[str, list[dict]] = {}
    for task in tasks:
        view = _shift_view(task)
        by_date.setdefault(view["scheduled_date"], []).append(view)
    ahead: list[str] = []
    cursor = today + timedelta(days=1)
    while True:
        rows = by_date.get(cursor.isoformat())
        if not rows:
            break
        if any(row["status"] != "completed" for row in rows):
            break
        ahead.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return ahead


def _shift_key(version_id: str, today: date, ahead: list[str], changes: list[dict], views: list[dict]) -> str:
    import hashlib

    payload = {
        "version": version_id,
        "today": today.isoformat(),
        "ahead": ahead,
        "changes": sorted([(item["task_id"], item["from_date"], item["to_date"]) for item in changes]),
        "tasks": sorted(
            [
                (item["id"], item["status"], item["scheduled_date"], item["completed_at"] or "")
                for item in views
            ]
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def compute_shift(
    tasks: list[ScheduleTask | dict],
    today: date | None = None,
    version_id: str = "",
) -> dict:
    today = today or today_local()
    views = [_shift_view(task) for task in tasks]
    ahead = completed_ahead_dates(tasks, today)
    tomorrow = today + timedelta(days=1)
    if not ahead:
        return {
            "available": False,
            "ahead_dates": [],
            "shift_days": 0,
            "changes": [],
            "days": [],
            "task_count": 0,
            "estimated_minutes": 0,
            "planned_end_date": None,
            "preview_key": None,
            "message": None,
        }
    shift_days = len(ahead)
    cutoff = ahead[-1]
    changes: list[dict] = []
    for view in views:
        if view["status"] == "completed":
            continue
        if view["scheduled_date"] <= cutoff:
            continue
        old_date = parse_iso_date(view["scheduled_date"])
        if old_date is None:
            continue
        new_date = old_date - timedelta(days=shift_days)
        if new_date < tomorrow:
            new_date = tomorrow
        if new_date == old_date:
            continue
        changes.append(
            {
                "task_id": view["id"],
                "task_title": view["task_title"],
                "task_type": view["task_type"],
                "from_date": view["scheduled_date"],
                "to_date": new_date.isoformat(),
                "estimated_minutes": view["estimated_minutes"],
            }
        )
    if not changes:
        return {
            "available": False,
            "ahead_dates": ahead,
            "shift_days": shift_days,
            "changes": [],
            "days": [],
            "task_count": 0,
            "estimated_minutes": 0,
            "planned_end_date": None,
            "preview_key": None,
            "message": None,
        }
    day_map: dict[str, dict] = {}
    for item in changes:
        day = day_map.setdefault(
            item["to_date"],
            {
                "date": item["to_date"],
                "from_dates": [],
                "task_count": 0,
                "estimated_minutes": 0,
            },
        )
        if item["from_date"] not in day["from_dates"]:
            day["from_dates"].append(item["from_date"])
        day["task_count"] += 1
        day["estimated_minutes"] += item["estimated_minutes"]
    days = sorted(day_map.values(), key=lambda item: item["date"])
    final_dates = [
        {item["task_id"]: item["to_date"] for item in changes}.get(
            view["id"], view["scheduled_date"]
        )
        for view in views
    ]
    planned_end = max(final_dates) if final_dates else None
    return {
        "available": True,
        "ahead_dates": ahead,
        "shift_days": shift_days,
        "changes": changes,
        "days": days,
        "task_count": len(changes),
        "estimated_minutes": sum(item["estimated_minutes"] for item in changes),
        "planned_end_date": planned_end,
        "preview_key": _shift_key(version_id, today, ahead, changes, views),
        "message": (
            f"{parse_iso_date(ahead[-1]).month} 月 {parse_iso_date(ahead[-1]).day} 日的任务你已经提前学完。"
            f"要把后面的内容整体前移 {shift_days} 天吗？"
        ),
    }


def shift_preview(db: Session, version: LearningPlanVersion, today: date | None = None) -> dict:
    today = today or today_local()
    tasks = tasks_for_version(db, version.id)
    result = compute_shift(tasks, today, version.id)
    config = schedule_config(version)
    dismissals = config.get(SHIFT_DISMISSALS_KEY) or []
    if result.get("preview_key") and result["preview_key"] in dismissals:
        result["available"] = False
        result["dismissed"] = True
    if config.get(LAST_SHIFT_KEY) and config.get(LAST_SHIFT_KEY) == result.get("preview_key"):
        result["available"] = False
        result["applied"] = True
    return result


def apply_shift(
    db: Session, plan: LearningPlan, version: LearningPlanVersion, preview_key: str, today: date | None = None
) -> dict:
    today = today or today_local()
    config = schedule_config(version)
    if preview_key and config.get(LAST_SHIFT_KEY) == preview_key:
        return {"duplicate": True, "applied": 0, "preview": compute_shift(tasks_for_version(db, version.id), today, version.id)}
    preview = shift_preview(db, version, today)
    if not preview.get("available"):
        if preview.get("dismissed"):
            raise ScheduleError("schedule_shift_dismissed", "这一批提前完成已选择保持原安排", 409)
        raise ScheduleError("schedule_shift_not_available", "现在没有可以前移的任务", 409)
    if not preview_key or preview_key != preview.get("preview_key"):
        raise ScheduleError("schedule_shift_preview_stale", "安排已变化，请重新预览调整", 409)
    changes = {item["task_id"]: item["to_date"] for item in preview["changes"]}
    update_task_dates(db, version, changes)
    config = schedule_config(version)
    config[LAST_SHIFT_KEY] = preview_key
    config["last_shift_applied_at"] = local_now_iso()
    version.schedule_json = json.dumps(config, ensure_ascii=False)
    plan.updated_at = now_iso()
    db.flush()
    tasks = tasks_for_version(db, version.id)
    return {
        "duplicate": False,
        "applied": len(changes),
        "version_id": version.id,
        "applied_changes": preview["changes"],
        "planned_end_date": config.get("planned_end_date"),
        "tasks": [_task_payload(task, today) for task in tasks],
    }


def dismiss_shift(
    db: Session, version: LearningPlanVersion, preview_key: str, today: date | None = None
) -> dict:
    preview = shift_preview(db, version, today or today_local())
    key = preview_key or preview.get("preview_key")
    if not key:
        return {"dismissed": False, "preview_key": None}
    config = schedule_config(version)
    dismissals = [item for item in (config.get(SHIFT_DISMISSALS_KEY) or []) if item != key]
    dismissals.append(key)
    config[SHIFT_DISMISSALS_KEY] = dismissals[-50:]
    version.schedule_json = json.dumps(config, ensure_ascii=False)
    db.flush()
    return {"dismissed": True, "preview_key": key}


def next_learnable_task(tasks: list[dict]) -> dict | None:
    for task in tasks:
        if task.get("status") == "completed":
            continue
        if task.get("action_url"):
            return task
    return None


def _reschedule_context(
    db: Session,
    version: LearningPlanVersion,
    task: ScheduleTask,
    target: date,
    today: date,
) -> dict:
    tasks = tasks_for_version(db, version.id)
    target_minutes = sum(
        int(item.estimated_minutes or 0)
        for item in tasks
        if item.scheduled_date == target.isoformat() and item.id != task.id
    )
    target_minutes += int(task.estimated_minutes or 0)
    config = schedule_config(version)
    daily_minutes = int(config.get("daily_minutes") or DEFAULT_DAILY_MINUTES)
    return {
        "task_id": task.id,
        "task_title": task.task_title,
        "module_title": task.module_title,
        "task_type": task.task_type,
        "from_date": task.scheduled_date,
        "to_date": target.isoformat(),
        "estimated_minutes": int(task.estimated_minutes or 0),
        "target_day_minutes": target_minutes,
        "daily_minutes": daily_minutes,
        "over_capacity": bool(daily_minutes and target_minutes > daily_minutes),
        "state": task_state(task, today),
    }


def reschedule_preview(
    db: Session, version: LearningPlanVersion, task: ScheduleTask, value: str, today: date | None = None
) -> dict:
    today = today or today_local()
    if task.status == "completed":
        raise ScheduleError("schedule_task_completed", "已完成的任务不能改期", 409)
    target = parse_iso_date(value, "scheduled_date")
    if target is None:
        raise ScheduleError("schedule_date_invalid", "请选择目标日期", 422)
    if target < today:
        raise ScheduleError("schedule_target_past", "只能改到今天或未来日期", 422)
    return _reschedule_context(db, version, task, target, today)


def reschedule_task(
    db: Session,
    version: LearningPlanVersion,
    task: ScheduleTask,
    value: str,
    expected_date: str | None,
    today: date | None = None,
) -> dict:
    today = today or today_local()
    preview = reschedule_preview(db, version, task, value, today)
    if preview["to_date"] == task.scheduled_date:
        return {**preview, "duplicate": True, "moved": False, "message": "这项任务已在该日期"}
    if expected_date and expected_date != task.scheduled_date:
        raise ScheduleError("schedule_task_changed", "这项任务已被改期，请重新预览", 409)
    update_task_dates(db, version, {task.id: preview["to_date"]})
    config = schedule_config(version)
    return {
        **preview,
        "duplicate": False,
        "moved": True,
        "planned_end_date": config.get("planned_end_date"),
        "message": (
            f"已移至 {parse_iso_date(preview['to_date']).month} 月 "
            f"{parse_iso_date(preview['to_date']).day} 日"
        ),
    }