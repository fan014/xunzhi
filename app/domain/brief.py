from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import schedule as schedule_service
from app.domain.models import (
    Attempt,
    AttemptAnswer,
    LearnerProfileSnapshot,
    LearningPack,
    LearningPlan,
    LearningPlanVersion,
    PackPlanVersion,
    Question,
    SourceMaterial,
)

MAX_SESSIONS = 6


def local_date_of(value) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 10:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone().date()


def _pack_ids(db: Session, version: LearningPlanVersion) -> list[str]:
    return list(
        db.execute(
            select(PackPlanVersion.pack_id).where(
                PackPlanVersion.plan_version_id == version.id
            )
        ).scalars().all()
    )


def _task_item(task: dict) -> dict:
    return {
        "id": task.get("id"),
        "task_title": task.get("task_title") or task.get("module_title") or "",
        "task_type": task.get("task_type"),
        "module_title": task.get("module_title"),
        "scheduled_date": task.get("scheduled_date"),
        "completed_at": task.get("completed_at"),
        "completed_ahead": bool(task.get("completed_ahead")),
        "url": task.get("action_url"),
    }


def _classify_tasks(tasks: list[dict], target: date) -> tuple[dict, list[dict]]:
    grouped: dict[str, list[dict]] = {"scheduled": [], "makeup": [], "ahead": []}
    completed: list[dict] = []
    for task in tasks:
        if task.get("status") != "completed" or not task.get("completed_at"):
            continue
        if local_date_of(task.get("completed_at")) != target:
            continue
        item = _task_item(task)
        completed.append(item)
        scheduled = str(task.get("scheduled_date") or "")[:10]
        if scheduled == target.isoformat():
            grouped["scheduled"].append(item)
        elif scheduled and scheduled < target.isoformat():
            grouped["makeup"].append(item)
        else:
            grouped["ahead"].append(item)
    return grouped, completed


def _attempt_question_ids(db: Session, attempt: Attempt, pack: LearningPack | None) -> list[str]:
    if pack is not None:
        ids = schedule_service.attempt_question_ids(db, pack, attempt)
        if ids:
            return ids
    return list(
        db.execute(
            select(Question.id)
            .where(Question.pack_id == attempt.pack_id)
            .order_by(Question.ordinal)
        ).scalars().all()
    )


def _attempt_counts(db: Session, attempt: Attempt, question_ids: list[str]) -> dict:
    answers = {
        row.question_id: row.selected_index
        for row in db.execute(
            select(AttemptAnswer).where(AttemptAnswer.attempt_id == attempt.id)
        ).scalars().all()
    }
    answered = sum(
        1 for qid in question_ids if answers.get(qid) is not None
    )
    total = int(attempt.total_count or 0) or len(question_ids)
    correct = int(attempt.correct_count or 0)
    unanswered = max(0, total - answered) if total >= answered else int(attempt.unanswered_count or 0)
    return {
        "total": total,
        "answered": answered,
        "correct": correct,
        "unanswered": unanswered,
        "wrong": max(0, answered - correct),
    }


def _earlier_question_ids(db: Session, pack_ids: list[str]) -> dict[str, str]:
    if not pack_ids:
        return {}
    rows = db.execute(
        select(AttemptAnswer.question_id, Attempt.submitted_at, Attempt.id)
        .join(Attempt, Attempt.id == AttemptAnswer.attempt_id)
        .where(Attempt.pack_id.in_(pack_ids), Attempt.status == "submitted")
    ).all()
    earliest: dict[str, str] = {}
    for question_id, submitted_at, _attempt_id in rows:
        if not submitted_at:
            continue
        current = earliest.get(question_id)
        if current is None or submitted_at < current:
            earliest[question_id] = submitted_at
    return earliest


def _session_label(db: Session, attempt: Attempt, task_titles: dict[str, str]) -> str:
    if attempt.schedule_task_id and attempt.schedule_task_id in task_titles:
        title = task_titles[attempt.schedule_task_id]
        for prefix in ("练习：", "练习:"):
            if title.startswith(prefix):
                title = title[len(prefix):]
        parts = [item.strip() for item in title.split(" · ") if item.strip()]
        if parts:
            return parts[0]
        return title
    pack = db.get(LearningPack, attempt.pack_id)
    return pack.display_title if pack is not None else "本次练习"


def _knowledge_url(
    db: Session,
    attempt: Attempt,
    question_ids: list[str],
    page_index: dict[str, list[dict]] | None = None,
) -> str | None:
    if not question_ids:
        return None
    question = db.get(Question, question_ids[0])
    module_id = question.module_id if question is not None else None
    if module_id:
        pages = (page_index or {}).get(module_id) or []
        page_id = pages[0].get("page_id") if pages else None
        suffix = f"&page_id={page_id}" if page_id else ""
        return f"/packs/{attempt.pack_id}?module_id={module_id}&view=modules{suffix}"
    return f"/packs/{attempt.pack_id}"


def _ready_snapshots(db: Session, attempt_ids: list[str]) -> list[LearnerProfileSnapshot]:
    if not attempt_ids:
        return []
    rows = db.execute(
        select(LearnerProfileSnapshot)
        .where(
            LearnerProfileSnapshot.attempt_id.in_(attempt_ids),
            LearnerProfileSnapshot.status == "ready",
        )
        .order_by(
            LearnerProfileSnapshot.completed_at.desc(),
            LearnerProfileSnapshot.created_at.desc(),
        )
    ).scalars().all()
    latest: dict[str, LearnerProfileSnapshot] = {}
    for row in rows:
        latest.setdefault(row.source_id, row)
    return list(latest.values())


def _advice_from_snapshots(
    db: Session,
    snapshots: list[LearnerProfileSnapshot],
    attempt: Attempt | None,
    question_ids: list[str],
    page_index: dict[str, list[dict]] | None = None,
) -> list[dict]:
    advice: list[dict] = []
    url = (
        _knowledge_url(db, attempt, question_ids, page_index)
        if attempt is not None
        else None
    )
    for snapshot in snapshots:
        source = db.get(SourceMaterial, snapshot.source_id) if snapshot.source_id else None
        source_name = source.display_name if source is not None else "资料已删除"
        try:
            result = json.loads(snapshot.result_json or "{}")
        except (TypeError, ValueError):
            result = {}
        for finding in (result.get("findings") or [])[:2]:
            advice.append(
                {
                    "source_name": source_name,
                    "knowledge_point": finding.get("knowledge_point") or "",
                    "text": finding.get("judgement") or "",
                    "url": url,
                }
            )
        for step in (result.get("route_steps") or [])[:1]:
            advice.append(
                {
                    "source_name": source_name,
                    "knowledge_point": step.get("knowledge_point") or "",
                    "text": step.get("instruction") or "",
                    "url": url,
                }
            )
        if len(advice) >= 3:
            break
    return advice[:3]


def _summary(
    counts: dict,
    practice: dict,
    reading_titles: list[str],
) -> str:
    if not counts["completed"] and not practice["sessions"]:
        return ""
    parts: list[str] = []
    if counts["completed"]:
        line = f"今天完成了 {counts['completed']} 项"
        extras = []
        if counts["ahead"]:
            extras.append(f"其中 {counts['ahead']} 项是提前学的")
        if counts["makeup"]:
            extras.append(f"补完 {counts['makeup']} 项以前的安排")
        if extras:
            line += "，" + "，".join(extras)
        parts.append(line)
    if practice["sessions"]:
        label = practice["sessions"][0]["label"]
        total = practice["total_questions"]
        correct = practice["correct"]
        if practice["first_time"]:
            parts.append(f"今天完成了首次练习：{label}这组 {total} 题里答对了 {correct} 题")
        elif len(practice["sessions"]) > 1:
            parts.append(
                f"今天完成了 {len(practice['sessions'])} 次练习，共 {total} 题里答对了 {correct} 题"
            )
        else:
            parts.append(f"{label}这组 {total} 题里答对了 {correct} 题")
        if practice["unanswered"]:
            parts.append(f"还有 {practice['unanswered']} 题没作答")
        if practice["redo_questions"]:
            parts.append(
                f"其中 {practice['redo_questions']} 题是重做，{practice['new_questions']} 题是这次新做的"
            )
    elif reading_titles:
        parts.append("没有练习，主要读了 " + "、".join(reading_titles[:2]))
    next_step = practice.get("next_step")
    if next_step:
        parts.append(next_step["text"])
    return "。".join(item.rstrip("。") for item in parts) + "。"


def build_brief(
    db: Session,
    plan: LearningPlan | None,
    version: LearningPlanVersion | None,
    target: date,
    *,
    tasks: list[dict],
    progress: dict,
    next_task: dict | None = None,
    today: date | None = None,
    page_index: dict[str, list[dict]] | None = None,
) -> dict:
    today = today or schedule_service.today_local()
    grouped, completed = _classify_tasks(tasks, target)
    counts = {
        "completed": len(completed),
        "scheduled": len(grouped["scheduled"]),
        "makeup": len(grouped["makeup"]),
        "ahead": len(grouped["ahead"]),
    }
    pack_ids = _pack_ids(db, version) if version is not None else []
    attempts: list[Attempt] = []
    if pack_ids:
        attempts = list(
            db.execute(
                select(Attempt)
                .where(
                    Attempt.pack_id.in_(pack_ids),
                    Attempt.status == "submitted",
                )
                .order_by(Attempt.submitted_at, Attempt.id)
            ).scalars().all()
        )
    day_attempts = [
        attempt
        for attempt in attempts
        if local_date_of(attempt.submitted_at) == target
    ]

    task_titles = {
        str(task.get("id")): str(task.get("task_title") or "")
        for task in tasks
        if task.get("id")
    }
    packs = {
        pack.id: pack
        for pack in db.execute(
            select(LearningPack).where(LearningPack.id.in_(pack_ids or [""]))
        ).scalars().all()
    }
    earlier = _earlier_question_ids(db, pack_ids)
    sessions = []
    all_question_ids: list[str] = []
    totals = {"total": 0, "answered": 0, "correct": 0, "unanswered": 0}
    for attempt in day_attempts:
        pack = packs.get(attempt.pack_id)
        if pack is None:
            pack = db.get(LearningPack, attempt.pack_id)
        question_ids = _attempt_question_ids(db, attempt, pack)
        counts_by_question = _attempt_counts(db, attempt, question_ids)
        redo = sum(
            1
            for qid in question_ids
            if earlier.get(qid) and earlier[qid] < (attempt.submitted_at or "")
        )
        all_question_ids.extend(question_ids)
        totals["total"] += counts_by_question["total"]
        totals["answered"] += counts_by_question["answered"]
        totals["correct"] += counts_by_question["correct"]
        totals["unanswered"] += counts_by_question["unanswered"]
        sessions.append(
            {
                "attempt_id": attempt.id,
                "pack_id": attempt.pack_id,
                "task_id": attempt.schedule_task_id,
                "label": _session_label(db, attempt, task_titles),
                "submitted_at": attempt.submitted_at,
                "total": counts_by_question["total"],
                "answered": counts_by_question["answered"],
                "correct": counts_by_question["correct"],
                "unanswered": counts_by_question["unanswered"],
                "accuracy": (
                    round(counts_by_question["correct"] / counts_by_question["answered"], 4)
                    if counts_by_question["answered"]
                    else None
                ),
                "redo_questions": redo,
                "new_questions": max(0, len(set(question_ids)) - redo),
                "url": _knowledge_url(db, attempt, question_ids, page_index),
            }
        )

    first_session_at = min(
        (attempt.submitted_at or "") for attempt in day_attempts
    ) if day_attempts else None
    previous_attempts = [
        attempt
        for attempt in attempts
        if first_session_at and (attempt.submitted_at or "") < first_session_at
    ]
    previous_accuracy = None
    if previous_attempts:
        previous = previous_attempts[-1]
        previous_pack = packs.get(previous.pack_id) or db.get(LearningPack, previous.pack_id)
        previous_ids = _attempt_question_ids(db, previous, previous_pack)
        previous_counts = _attempt_counts(db, previous, previous_ids)
        if previous_counts["answered"]:
            previous_accuracy = round(
                previous_counts["correct"] / previous_counts["answered"], 4
            )

    unique_question_ids = list(dict.fromkeys(all_question_ids))
    redo_total = sum(session["redo_questions"] for session in sessions)
    practice = {
        "sessions": sessions[:MAX_SESSIONS],
        "total_attempts": len(sessions),
        "total_questions": totals["total"],
        "answered": totals["answered"],
        "correct": totals["correct"],
        "unanswered": totals["unanswered"],
        "accuracy": (
            round(totals["correct"] / totals["answered"], 4)
            if totals["answered"]
            else None
        ),
        "previous_accuracy": previous_accuracy,
        "first_time": bool(sessions) and not previous_attempts,
        "redo_questions": redo_total,
        "new_questions": max(0, len(unique_question_ids) - redo_total),
    }

    snapshots = _ready_snapshots(db, [attempt.id for attempt in day_attempts])
    first_attempt = day_attempts[0] if day_attempts else None
    advice = _advice_from_snapshots(
        db,
        snapshots,
        first_attempt,
        _attempt_question_ids(
            db,
            first_attempt,
            packs.get(first_attempt.pack_id) or db.get(LearningPack, first_attempt.pack_id),
        )
        if first_attempt is not None
        else [],
        page_index,
    )
    next_step = None
    if sessions:
        if advice:
            point = advice[0].get("knowledge_point") or "今天的错题"
            next_step = {
                "text": f"下次可以先回看「{point}」，再做一道新情境题",
                "url": advice[0].get("url"),
            }
        elif totals["answered"] and totals["correct"] == totals["answered"]:
            next_step = {
                "text": "下次可以换一道新情境题再验证一次",
                "url": sessions[0].get("url"),
            }
        else:
            next_step = {
                "text": "下次可以先回看今天的错题，再做一道新情境题",
                "url": sessions[0].get("url"),
            }
    practice["advice"] = advice
    practice["next_step"] = next_step

    reading_titles = [
        item["task_title"]
        for item in completed
        if item["task_type"] in {"reading", "review"}
    ]
    summary = _summary(counts, practice, reading_titles)
    empty = not completed and not sessions
    if empty:
        summary = "这一天没有留下学习记录。可以选一个日期看看当天安排，或回到今天继续。"

    return {
        "date": target.isoformat(),
        "is_today": target == today,
        "empty": empty,
        "summary": summary,
        "counts": counts,
        "completed_tasks": grouped,
        "practice": practice,
        "advice": advice,
        "next_step": next_step,
        "next_task": next_task,
        "progress": progress,
        "plan_id": plan.id if plan is not None else None,
        "version_id": version.id if version is not None else None,
    }