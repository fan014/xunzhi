from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from sqlalchemy import select

from app.domain import mastery, plans
from app.domain import schedule as schedule_service
from app.domain.models import (
    Attempt,
    AttemptAnswer,
    Citation,
    DiagnosticAnswer,
    DiagnosticQuestion,
    DiagnosticSession,
    KnowledgePoint,
    LearnerProfileSnapshot,
    LearningEvidence,
    LearningPack,
    PackSource,
    Question,
    ScheduleTask,
    SelfAssessment,
    SourceMaterial,
    now_iso,
)
from app.infra.db import SessionLocal
from app.infra.ids import new_id
from app.infra.model_client import (
    ModelError,
    ModelRequest,
    TextPayload,
    get_model_client,
    is_fake_client,
)

logger = logging.getLogger("xunzhi.assessment")

LEARNER_KEY = "local"
PROFILE_RULE_VERSION = "profile_v1"
MAX_HISTORY_ATTEMPTS = 2
PENDING_REUSE_SECONDS = 120

FINDING_KINDS = {"observation", "possible_misunderstanding"}
ROUTE_ACTIONS = {
    "review_material": "回看资料",
    "compare_explanation": "对照讲解",
    "new_question": "做同考点新题",
    "retest": "补测",
    "brief_review": "简短回顾",
}

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


class AssessmentError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def load_assessment_prompt() -> str:
    return (_project_root() / "app" / "prompts" / "system_assessment.md").read_text(encoding="utf-8")


def _parse_json(text: str | None) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise AssessmentError("assessment_invalid_structure", "模型返回内容为空")
    candidates = [raw]
    fenced = _FENCE.match(raw)
    if fenced:
        candidates.append(fenced.group(1).strip())
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
        raise AssessmentError("assessment_invalid_structure", "模型返回内容不是 JSON 对象")
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict):
            return data
    raise AssessmentError("assessment_invalid_structure", "模型返回内容不是合法 JSON")


def _scope_for_attempt(db, attempt: Attempt, pack: LearningPack) -> list[dict]:
    links = db.execute(
        select(PackSource).where(PackSource.pack_id == pack.id).order_by(PackSource.source_id)
    ).scalars().all()
    return [
        {
            "source_id": link.source_id,
            "page_start": link.page_start,
            "page_end": link.page_end,
            "display_name": link.source_name_snapshot,
        }
        for link in links
    ]


def _primary_scope(scope: list[dict]) -> dict:
    if not scope:
        raise AssessmentError("assessment_scope_missing", "本次答卷没有可分析的资料范围", 422)
    return scope[0]


def _current_fingerprint(db, source_id: str, page_start=None, page_end=None) -> str:
    base, _ = plans._evidence_fingerprint(db, source_id, page_start, page_end)
    return plans._sha({"evidence": base, "rule": PROFILE_RULE_VERSION})


def current_fingerprint(db, source_id: str, page_start=None, page_end=None) -> str:
    return _current_fingerprint(db, source_id, page_start, page_end)


def _options(question: Question) -> list[str]:
    try:
        values = json.loads(question.options_json)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        if isinstance(item, dict):
            key = str(item.get("key") or "").strip()
            text = str(item.get("text") or item.get("label") or "").strip()
            result.append(f"{key}. {text}" if key else text)
        else:
            result.append(str(item))
    return result


def _citation_text(db, evidence: LearningEvidence) -> dict | None:
    citation = db.get(Citation, evidence.citation_id) if evidence.citation_id else None
    source = db.get(SourceMaterial, evidence.source_id) if evidence.source_id else None
    if citation is None or source is None or source.status != "active":
        return None
    return {
        "citation_id": citation.id,
        "source_id": citation.source_id,
        "page_no": citation.page_no,
        "quote": (citation.quote_snapshot or citation.claim_text or "")[:240],
    }


def _evidence_item(
    db,
    evidence: LearningEvidence,
    *,
    earlier_question_ids: set[str],
    point: KnowledgePoint,
    origin: str = "current",
) -> dict | None:
    source = db.get(SourceMaterial, evidence.source_id) if evidence.source_id else None
    if source is None or source.status != "active":
        return None
    question = db.get(Question, evidence.question_id)
    attempt = db.get(Attempt, evidence.attempt_id)
    if question is None or attempt is None or attempt.status != "submitted":
        return None
    citation = _citation_text(db, evidence)
    if citation is None:
        return None
    answer = db.get(AttemptAnswer, (evidence.attempt_id, evidence.question_id))
    options = _options(question)
    selected = answer.selected_index if answer is not None else None
    is_original_redo = question.id in earlier_question_ids
    is_new_question = bool(question.origin_question_id) and not is_original_redo
    return {
        "evidence_id": evidence.id,
        "origin": origin,
        "knowledge_point_id": point.id,
        "knowledge_point": point.display_name,
        "question_id": question.id,
        "question_ordinal": question.ordinal,
        "module_id": question.module_id,
        "pack_id": evidence.pack_id,
        "attempt_id": evidence.attempt_id,
        "stem": question.stem,
        "selected_index": selected,
        "selected_text": options[selected] if selected is not None and 0 <= selected < len(options) else None,
        "answer_index": question.answer_index,
        "correct_text": options[question.answer_index] if 0 <= question.answer_index < len(options) else None,
        "result": evidence.result,
        "unanswered": evidence.result == "unanswered",
        "is_original_redo": is_original_redo,
        "is_new_question": is_new_question,
        "source_type": evidence.source_type,
        "submitted_at": evidence.submitted_at,
        "citation": citation,
        "mastery_state": mastery.evaluate_point_mastery(db, point, source_scope=[point.source_id]).state,
    }


def _earlier_question_ids(db, source_id: str, current_attempt_id: str) -> set[str]:
    rows = db.execute(
        select(LearningEvidence.question_id).where(
            LearningEvidence.source_id == source_id,
            LearningEvidence.attempt_id != current_attempt_id,
        )
    ).scalars().all()
    return set(rows)


def _diagnosis_basis(db, source_id: str) -> list[dict]:
    sessions = db.execute(
        select(DiagnosticSession)
        .where(DiagnosticSession.source_id == source_id, DiagnosticSession.status == "confirmed")
        .order_by(DiagnosticSession.confirmed_at.desc(), DiagnosticSession.created_at.desc())
        .limit(2)
    ).scalars().all()
    items = []
    for session in sessions:
        rows = db.execute(
            select(DiagnosticAnswer, DiagnosticQuestion)
            .join(DiagnosticQuestion, DiagnosticQuestion.id == DiagnosticAnswer.question_id)
            .where(DiagnosticAnswer.session_id == session.id)
            .order_by(DiagnosticAnswer.created_at)
        ).all()
        for answer, question in rows:
            items.append(
                {
                    "source": "diagnosis",
                    "session_id": session.id,
                    "kind": question.kind,
                    "question": question.stem,
                    "answer_index": answer.selected_index,
                    "status": answer.status,
                    "goal": session.target_goal or "",
                }
            )
    return items[:6]


def _self_assessment_basis(db, source_id: str) -> list[dict]:
    rows = db.execute(
        select(SelfAssessment, KnowledgePoint)
        .join(KnowledgePoint, KnowledgePoint.id == SelfAssessment.knowledge_point_id, isouter=True)
        .where(KnowledgePoint.source_id == source_id)
        .order_by(SelfAssessment.created_at.desc())
        .limit(5)
    ).all()
    return [
        {
            "source": "self_assessment",
            "knowledge_point_id": point.id if point is not None else None,
            "knowledge_point": point.display_name if point is not None else "",
            "text": item.text,
        }
        for item, point in rows
    ]


def _attempt_question_ids(db, attempt: Attempt, pack: LearningPack) -> list[str]:
    if attempt.schedule_task_id:
        task = db.get(ScheduleTask, attempt.schedule_task_id)
        if task is None:
            return []
        return schedule_service.ensure_task_question_ids(db, task, pack)
    return list(
        db.execute(
            select(Question.id).where(Question.pack_id == pack.id).order_by(Question.ordinal)
        ).scalars().all()
    )


def _current_evidence_by_source(
    db, attempt: Attempt, scope_by_source: dict[str, dict], allowed_question_ids: set[str]
) -> dict[str, list[LearningEvidence]]:
    rows = db.execute(
        select(LearningEvidence).where(
            LearningEvidence.attempt_id == attempt.id,
            LearningEvidence.reliability == "confirmed",
            LearningEvidence.knowledge_point_id.is_not(None),
            LearningEvidence.source_type.in_(("practice", "retry")),
        )
    ).scalars().all()
    grouped: dict[str, list[LearningEvidence]] = {}
    for row in rows:
        if row.question_id not in allowed_question_ids:
            continue
        if row.source_id not in scope_by_source:
            continue
        grouped.setdefault(row.source_id, []).append(row)
    return grouped


def _history_rows(db, source_id: str, attempt_id: str) -> list[LearningEvidence]:
    return list(
        db.execute(
            select(LearningEvidence).where(
                LearningEvidence.source_id == source_id,
                LearningEvidence.attempt_id != attempt_id,
                LearningEvidence.reliability == "confirmed",
                LearningEvidence.knowledge_point_id.is_not(None),
                LearningEvidence.source_type.in_(("practice", "retry")),
            )
        ).scalars().all()
    )


def _select_history(rows: list[LearningEvidence]) -> tuple[list[LearningEvidence], list[str]]:
    history_attempts: list[str] = []
    for row in sorted(rows, key=lambda item: item.submitted_at or "", reverse=True):
        if row.attempt_id in history_attempts:
            continue
        history_attempts.append(row.attempt_id)
        if len(history_attempts) >= MAX_HISTORY_ATTEMPTS:
            break
    selected: list[LearningEvidence] = []
    for attempt_id in history_attempts:
        attempt_rows = [row for row in rows if row.attempt_id == attempt_id]
        wrong = [row for row in attempt_rows if row.result == "wrong"]
        retry = [row for row in attempt_rows if row.source_type == "retry"]
        chosen = wrong + [row for row in retry if row not in wrong]
        seen_points: set[str] = set()
        for row in attempt_rows:
            if row in chosen or row.knowledge_point_id in seen_points:
                continue
            seen_points.add(row.knowledge_point_id)
            chosen.append(row)
        selected.extend(chosen)
    return selected, history_attempts


def _attempt_source_key(
    attempt_id: str,
    source_id: str,
    page_start,
    page_end,
    current_evidence_ids: list[str],
) -> str:
    return plans._sha(
        {
            "attempt": attempt_id,
            "source": source_id,
            "page_start": page_start,
            "page_end": page_end,
            "evidence": sorted(current_evidence_ids),
            "rule": PROFILE_RULE_VERSION,
        }
    )


def _build_source_input(
    db,
    attempt: Attempt,
    pack: LearningPack,
    scope: dict,
    current_rows: list[LearningEvidence],
    question_ordinals: dict[str, int],
) -> dict | None:
    source_id = scope["source_id"]
    page_start = scope["page_start"]
    page_end = scope["page_end"]
    point_rows = db.execute(
        select(KnowledgePoint).where(
            KnowledgePoint.source_id == source_id, KnowledgePoint.status == "active"
        )
    ).scalars().all()
    points = {point.id: point for point in point_rows}
    mastery_by_point = {
        point.id: mastery.evaluate_point_mastery(db, point, source_scope=[source_id])
        for point in point_rows
    }

    earlier = _earlier_question_ids(db, source_id, attempt.id)
    evidence_items: list[dict] = []
    seen_evidence: set[str] = set()
    current_sorted = sorted(
        current_rows,
        key=lambda row: (question_ordinals.get(row.question_id, 10**9), row.id),
    )
    for row in current_sorted:
        point = points.get(row.knowledge_point_id)
        if point is None or row.id in seen_evidence:
            continue
        item = _evidence_item(
            db, row, earlier_question_ids=earlier, point=point, origin="current"
        )
        if item is None:
            continue
        seen_evidence.add(row.id)
        evidence_items.append(item)

    history_rows, history_attempts = _select_history(_history_rows(db, source_id, attempt.id))
    for row in history_rows:
        point = points.get(row.knowledge_point_id)
        if point is None or row.id in seen_evidence:
            continue
        item = _evidence_item(
            db, row, earlier_question_ids=earlier, point=point, origin="history"
        )
        if item is None:
            continue
        seen_evidence.add(row.id)
        evidence_items.append(item)

    current_items = [item for item in evidence_items if item["origin"] == "current"]
    if not current_items:
        return None

    used_points = {item["knowledge_point_id"] for item in evidence_items}
    payload = {
        "learner_key": LEARNER_KEY,
        "rule_version": PROFILE_RULE_VERSION,
        "source": {
            "source_id": source_id,
            "display_name": scope["display_name"],
            "page_start": page_start,
            "page_end": page_end,
            "question_count": len({row.question_id for row in current_rows}),
            "evidence_count": len(current_rows),
        },
        "current_attempt": {
            "attempt_id": attempt.id,
            "pack_id": pack.id,
            "task_id": attempt.schedule_task_id,
            "submitted_at": attempt.submitted_at,
            "correct": attempt.correct_count,
            "wrong": attempt.wrong_count,
            "unanswered": attempt.unanswered_count,
            "total": attempt.total_count,
        },
        "history_attempts": history_attempts,
        "evidence_summary": {
            "current": len(current_items),
            "history": len(evidence_items) - len(current_items),
        },
        "points": [
            {
                "knowledge_point_id": point_id,
                "display_name": points[point_id].display_name,
                "mastery": mastery_by_point[point_id].to_dict(),
            }
            for point_id in sorted(used_points)
        ],
        "evidence": evidence_items,
        "diagnosis": _diagnosis_basis(db, source_id),
        "self_assessment": _self_assessment_basis(db, source_id),
        "fingerprint": _current_fingerprint(db, source_id, page_start, page_end),
        "cache_key": _attempt_source_key(
            attempt.id, source_id, page_start, page_end, [row.id for row in current_rows]
        ),
    }
    return payload


def _attempt_coverage(
    db,
    allowed_ids: list[str],
    grouped: dict[str, list[LearningEvidence]],
    scope_by_source: dict[str, dict],
) -> dict:
    questions = (
        list(
            db.execute(select(Question).where(Question.id.in_(allowed_ids))).scalars().all()
        )
        if allowed_ids
        else []
    )
    by_id = {question.id: question for question in questions}
    order = sorted(allowed_ids, key=lambda qid: by_id.get(qid).ordinal if qid in by_id else 10**9)
    covered = {row.question_id for rows in grouped.values() for row in rows}
    missing = [qid for qid in order if qid not in covered]
    sources = []
    for source_id, rows in grouped.items():
        scope = scope_by_source[source_id]
        sources.append(
            {
                "source_id": source_id,
                "source_name": scope["display_name"],
                "covered_questions": len({row.question_id for row in rows}),
                "covered_evidence": len(rows),
            }
        )
    pending_verification = []
    for qid in missing:
        question = by_id.get(qid)
        if question is None:
            continue
        pending_verification.append(
            {
                "question_id": qid,
                "stem": question.stem,
                "knowledge_point": question.knowledge_point,
                "reason": "缺少可核对的作答证据",
            }
        )
    return {
        "total_questions": len(allowed_ids),
        "covered_questions": len(covered),
        "uncovered_questions": len(missing),
        "source_count": len(grouped),
        "sources": sources,
        "pending_verification": pending_verification,
    }


def _build_all_inputs(db, attempt: Attempt) -> tuple[LearningPack, list[dict], dict]:
    pack = db.get(LearningPack, attempt.pack_id)
    if pack is None:
        raise AssessmentError("assessment_pack_missing", "本次答卷对应的学习计划不存在", 404)
    if attempt.status != "submitted":
        raise AssessmentError("assessment_attempt_not_submitted", "只有已交卷的练习才能生成档案", 409)
    scopes = _scope_for_attempt(db, attempt, pack)
    if not scopes:
        raise AssessmentError("assessment_scope_missing", "本次答卷没有可分析的资料范围", 422)
    scope_by_source = {scope["source_id"]: scope for scope in scopes}
    allowed_ids = _attempt_question_ids(db, attempt, pack)
    allowed_set = set(allowed_ids)
    grouped = _current_evidence_by_source(db, attempt, scope_by_source, allowed_set)
    question_ordinals = {
        question.id: question.ordinal
        for question in db.execute(
            select(Question).where(
                Question.pack_id == pack.id, Question.id.in_(allowed_ids or [""])
            )
        ).scalars().all()
    }
    inputs = []
    for scope in scopes:
        rows = grouped.get(scope["source_id"])
        if not rows:
            continue
        payload = _build_source_input(db, attempt, pack, scope, rows, question_ordinals)
        if payload is not None:
            inputs.append(payload)
    coverage = _attempt_coverage(db, allowed_ids, grouped, scope_by_source)
    if not inputs:
        raise AssessmentError(
            "assessment_evidence_missing",
            "本次范围内没有可核对的作答证据，暂不能生成个人档案",
            422,
        )
    return pack, inputs, coverage


def build_input(db, attempt: Attempt, *, client=None, source_id: str | None = None) -> dict:
    _, inputs, _ = _build_all_inputs(db, attempt)
    if source_id:
        for item in inputs:
            if item["source"]["source_id"] == source_id:
                return item
        raise AssessmentError(
            "assessment_source_not_in_attempt",
            "本次答卷在这份资料上没有可核对的作答证据",
            422,
        )
    return inputs[0]


def _build_request(input_data: dict) -> ModelRequest:
    task = (
        "【个人学习测评】请根据下列已保存的作答事实生成结构化学习档案建议。\n"
        "不得改分数、不得补造作答、不得把未作答算作错题、不得把单次订正当成掌握稳定。\n"
        "所有 knowledge_point_id 和 evidence_ids 必须来自下方输入。\n"
        "【测评输入】\n"
        + json.dumps(input_data, ensure_ascii=False)
    )
    return ModelRequest(
        system_prompt=load_assessment_prompt(),
        task_text=task,
        text_items=[TextPayload(ref="ASSESSMENT_INPUT", description=json.dumps(input_data, ensure_ascii=False))],
        question_count=3,
    )


def _short_text(value, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def validate_output(data: dict, input_data: dict) -> dict:
    points = {item["knowledge_point_id"]: item for item in input_data["points"]}
    evidence = {item["evidence_id"]: item for item in input_data["evidence"]}
    if not points or not evidence:
        raise AssessmentError("assessment_evidence_missing", "测评输入缺少考点或证据")

    findings = []
    for raw in data.get("findings") or []:
        if not isinstance(raw, dict):
            raise AssessmentError("assessment_invalid_structure", "findings 必须为对象数组")
        point_id = raw.get("knowledge_point_id")
        if point_id not in points:
            raise AssessmentError("assessment_out_of_scope", "测评结论引用了本次输入之外的考点")
        kind = raw.get("kind")
        if kind not in FINDING_KINDS:
            raise AssessmentError("assessment_invalid_structure", "findings.kind 不在允许范围内")
        judgement = _short_text(raw.get("judgement") or raw.get("summary"), 200)
        if not judgement:
            raise AssessmentError("assessment_invalid_structure", "findings 缺少判断")
        evidence_ids = raw.get("evidence_ids") or []
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise AssessmentError("assessment_invalid_structure", "findings 缺少证据")
        resolved = []
        for evidence_id in evidence_ids:
            item = evidence.get(evidence_id)
            if item is None:
                raise AssessmentError("assessment_out_of_scope", "测评结论引用了本次输入之外的证据")
            if item["knowledge_point_id"] != point_id:
                raise AssessmentError("assessment_evidence_mismatch", "测评证据不属于该考点")
            resolved.append(evidence_id)
        if any(evidence[item]["result"] == "unanswered" for item in resolved) and any(
            token in judgement for token in ("答错", "错误", "做错")
        ):
            raise AssessmentError("assessment_unanswered_as_wrong", "未作答不能算作错题")
        if all(evidence[item]["is_original_redo"] for item in resolved) and any(
            token in judgement for token in ("已掌握", "掌握稳定", "相对稳定")
        ):
            raise AssessmentError("assessment_redo_as_stable", "单次订正不能当作掌握稳定")
        findings.append(
            {
                "knowledge_point_id": point_id,
                "knowledge_point": points[point_id]["display_name"],
                "kind": kind,
                "judgement": judgement,
                "evidence_ids": sorted(set(resolved)),
                "note": _short_text(raw.get("note") or raw.get("explanation"), 300),
            }
        )

    route_steps = []
    for index, raw in enumerate(data.get("route_steps") or [], start=1):
        if not isinstance(raw, dict):
            raise AssessmentError("assessment_invalid_structure", "route_steps 必须为对象数组")
        point_id = raw.get("knowledge_point_id")
        if point_id not in points:
            raise AssessmentError("assessment_out_of_scope", "学习路线引用了本次输入之外的考点")
        action = raw.get("action")
        if action not in ROUTE_ACTIONS:
            raise AssessmentError("assessment_invalid_structure", "学习路线动作不在允许范围内")
        basis = raw.get("basis_evidence_ids") or raw.get("evidence_ids") or []
        if not isinstance(basis, list) or not basis:
            raise AssessmentError("assessment_invalid_structure", "学习路线缺少依据")
        resolved = []
        for evidence_id in basis:
            item = evidence.get(evidence_id)
            if item is None:
                raise AssessmentError("assessment_out_of_scope", "学习路线引用了本次输入之外的证据")
            if item["knowledge_point_id"] != point_id:
                raise AssessmentError("assessment_evidence_mismatch", "学习路线证据不属于该考点")
            resolved.append(evidence_id)
        completion = _short_text(raw.get("completion") or raw.get("completion_standard"), 200)
        instruction = _short_text(raw.get("instruction") or raw.get("suggestion"), 240)
        if not completion or not instruction:
            raise AssessmentError("assessment_invalid_structure", "学习路线缺少建议动作或完成标准")
        route_steps.append(
            {
                "order": index,
                "knowledge_point_id": point_id,
                "knowledge_point": points[point_id]["display_name"],
                "action": action,
                "action_label": ROUTE_ACTIONS[action],
                "instruction": instruction,
                "basis_evidence_ids": sorted(set(resolved)),
                "completion": completion,
            }
        )

    unknowns = []
    for raw in data.get("unknowns") or []:
        if not isinstance(raw, dict):
            raise AssessmentError("assessment_invalid_structure", "unknowns 必须为对象数组")
        point_id = raw.get("knowledge_point_id")
        if point_id not in points:
            raise AssessmentError("assessment_out_of_scope", "待验证项引用了本次输入之外的考点")
        reason = _short_text(raw.get("reason"), 200)
        next_check = _short_text(raw.get("next_check") or raw.get("verify"), 200)
        if not reason or not next_check:
            raise AssessmentError("assessment_invalid_structure", "待验证项缺少原因或验证方式")
        unknowns.append(
            {
                "knowledge_point_id": point_id,
                "knowledge_point": points[point_id]["display_name"],
                "reason": reason,
                "next_check": next_check,
            }
        )

    if not findings and not route_steps and not unknowns:
        raise AssessmentError("assessment_invalid_structure", "测评结果没有可展示的结论")
    return {
        "findings": findings,
        "route_steps": route_steps,
        "unknowns": unknowns,
        "points": input_data["points"],
        "evidence": input_data["evidence"],
        "diagnosis": input_data["diagnosis"],
        "self_assessment": input_data["self_assessment"],
    }


def _snapshot_payload(db, snapshot: LearnerProfileSnapshot) -> dict:
    source = db.get(SourceMaterial, snapshot.source_id)
    pack = None
    if snapshot.attempt_id:
        attempt = db.get(Attempt, snapshot.attempt_id)
        pack = db.get(LearningPack, attempt.pack_id) if attempt is not None else None
    result = {}
    if snapshot.result_json:
        try:
            result = json.loads(snapshot.result_json)
        except (TypeError, json.JSONDecodeError):
            result = {}
    evidence = result.get("evidence") or []
    return {
        "id": snapshot.id,
        "learner_key": snapshot.learner_key,
        "source_id": snapshot.source_id,
        "source_name": source.display_name if source is not None else "资料已删除",
        "source_status": source.status if source is not None else "deleted",
        "page_start": snapshot.page_start,
        "page_end": snapshot.page_end,
        "attempt_id": snapshot.attempt_id,
        "pack_id": pack.id if pack is not None else None,
        "pack_title": pack.display_title if pack is not None else None,
        "evidence_ids": json.loads(snapshot.evidence_ids_json or "[]"),
        "evidence_fingerprint": snapshot.evidence_fingerprint,
        "status": snapshot.status,
        "rule_version": snapshot.rule_version,
        "created_at": snapshot.created_at,
        "completed_at": snapshot.completed_at,
        "retryable": snapshot.retryable,
        "failure": (
            {"code": snapshot.failure_code, "message": snapshot.failure_message}
            if snapshot.status == "failed"
            else None
        ),
        "findings": result.get("findings") or [],
        "route_steps": result.get("route_steps") or [],
        "unknowns": result.get("unknowns") or [],
        "points": result.get("points") or [],
        "evidence": evidence,
        "diagnosis": result.get("diagnosis") or [],
        "self_assessment": result.get("self_assessment") or [],
    }


def latest_for_attempt(db, attempt_id: str, *, status: str | None = None) -> LearnerProfileSnapshot | None:
    query = select(LearnerProfileSnapshot).where(LearnerProfileSnapshot.attempt_id == attempt_id)
    if status is not None:
        query = query.where(LearnerProfileSnapshot.status == status)
    return db.execute(
        query.order_by(LearnerProfileSnapshot.created_at.desc())
    ).scalars().first()


def latest_ready(db, source_id: str) -> LearnerProfileSnapshot | None:
    return db.execute(
        select(LearnerProfileSnapshot)
        .where(
            LearnerProfileSnapshot.source_id == source_id,
            LearnerProfileSnapshot.status == "ready",
        )
        .order_by(LearnerProfileSnapshot.completed_at.desc(), LearnerProfileSnapshot.created_at.desc())
    ).scalars().first()


def latest_effective(db, source_id: str, page_start=None, page_end=None) -> LearnerProfileSnapshot | None:
    fingerprint = _current_fingerprint(db, source_id, page_start, page_end)
    return db.execute(
        select(LearnerProfileSnapshot)
        .where(
            LearnerProfileSnapshot.source_id == source_id,
            LearnerProfileSnapshot.status == "ready",
            LearnerProfileSnapshot.evidence_fingerprint == fingerprint,
        )
        .order_by(LearnerProfileSnapshot.completed_at.desc(), LearnerProfileSnapshot.created_at.desc())
    ).scalars().first()


def _run_model(input_data: dict, client) -> dict:
    if client is None:
        client = get_model_client()
    result = client.generate(_build_request(input_data))
    if getattr(result, "finish_reason", None) == "length":
        raise AssessmentError("assessment_invalid_structure", "模型返回内容被截断，请重试")
    data = _parse_json(getattr(result, "content_text", ""))
    return validate_output(data, input_data)


def _latest_snapshot_for_source(
    db, attempt_id: str, source_id: str
) -> LearnerProfileSnapshot | None:
    return db.execute(
        select(LearnerProfileSnapshot)
        .where(
            LearnerProfileSnapshot.attempt_id == attempt_id,
            LearnerProfileSnapshot.source_id == source_id,
        )
        .order_by(LearnerProfileSnapshot.created_at.desc(), LearnerProfileSnapshot.id.desc())
    ).scalars().first()


def _reusable_snapshot(db, attempt: Attempt, input_data: dict) -> LearnerProfileSnapshot | None:
    expected = {item["evidence_id"] for item in input_data["evidence"]}
    rows = db.execute(
        select(LearnerProfileSnapshot)
        .where(
            LearnerProfileSnapshot.attempt_id == attempt.id,
            LearnerProfileSnapshot.source_id == input_data["source"]["source_id"],
            LearnerProfileSnapshot.status.in_(("ready", "pending")),
        )
        .order_by(LearnerProfileSnapshot.created_at.desc(), LearnerProfileSnapshot.id.desc())
    ).scalars().all()
    for row in rows:
        try:
            stored = set(json.loads(row.evidence_ids_json or "[]"))
        except (TypeError, json.JSONDecodeError):
            stored = set()
        if stored != expected:
            continue
        if row.page_start != input_data["source"]["page_start"]:
            continue
        if row.page_end != input_data["source"]["page_end"]:
            continue
        return row
    return None


def _generate_source(db, attempt: Attempt, input_data: dict, client) -> dict:
    snapshot = None
    try:
        snapshot = LearnerProfileSnapshot(
            id=new_id("profile"),
            learner_key=LEARNER_KEY,
            source_id=input_data["source"]["source_id"],
            page_start=input_data["source"]["page_start"],
            page_end=input_data["source"]["page_end"],
            attempt_id=attempt.id,
            evidence_ids_json=json.dumps(
                [item["evidence_id"] for item in input_data["evidence"]], ensure_ascii=False
            ),
            evidence_fingerprint=input_data["fingerprint"],
            status="pending",
            rule_version=PROFILE_RULE_VERSION,
            created_at=now_iso(),
        )
        db.add(snapshot)
        db.commit()
        validated = _run_model(input_data, client)
        snapshot.result_json = json.dumps(validated, ensure_ascii=False)
        snapshot.status = "ready"
        snapshot.completed_at = now_iso()
        snapshot.retryable = False
        snapshot.failure_code = None
        snapshot.failure_message = None
        db.commit()
        return _snapshot_payload(db, snapshot)
    except ModelError as exc:
        logger.warning(
            "assessment model failed attempt=%s source=%s code=%s",
            attempt.id,
            input_data["source"]["source_id"],
            exc.code,
        )
        return _failure_payload(db, snapshot, "assessment_model_failed", exc.message, retryable=True)
    except AssessmentError as exc:
        return _failure_payload(
            db,
            snapshot,
            exc.code,
            exc.message,
            retryable=exc.status >= 500
            or exc.code
            not in {"assessment_attempt_not_submitted", "assessment_pack_missing"},
        )
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception(
            "assessment failed attempt=%s source=%s",
            attempt.id,
            input_data["source"]["source_id"],
        )
        return _failure_payload(
            db, snapshot, "assessment_internal_error", "测评失败，请稍后重试", retryable=True
        )


def _source_entry(
    db,
    input_data: dict,
    snapshot: LearnerProfileSnapshot | None,
    coverage: dict,
    failure: dict | None = None,
) -> dict:
    source_id = input_data["source"]["source_id"]
    counts = next(
        (item for item in coverage.get("sources") or [] if item["source_id"] == source_id),
        {},
    )
    entry = {
        "source_id": source_id,
        "source_name": input_data["source"]["display_name"],
        "page_start": input_data["source"]["page_start"],
        "page_end": input_data["source"]["page_end"],
        "question_count": input_data["source"].get("question_count", 0),
        "covered_questions": counts.get("covered_questions", 0),
        "evidence_count": input_data["source"].get("evidence_count", 0),
        "current_evidence_count": input_data["evidence_summary"]["current"],
        "history_evidence_count": input_data["evidence_summary"]["history"],
        "snapshot_id": None,
        "status": "missing",
        "retryable": False,
        "completed_at": None,
        "failure": None,
        "findings": [],
        "route_steps": [],
        "unknowns": [],
        "points": [],
        "evidence": [],
    }
    if snapshot is not None:
        payload = _snapshot_payload(db, snapshot)
        entry.update(
            {
                "snapshot_id": payload["id"],
                "status": payload["status"],
                "retryable": payload["retryable"],
                "completed_at": payload["completed_at"],
                "failure": payload["failure"],
                "findings": payload["findings"],
                "route_steps": payload["route_steps"],
                "unknowns": payload["unknowns"],
                "points": payload["points"],
                "evidence": payload["evidence"],
            }
        )
    elif failure is not None:
        entry["status"] = "failed"
        entry["retryable"] = failure.get("retryable", True)
        entry["failure"] = failure.get("failure")
    return entry


def _merge_points(entries: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for entry in entries:
        for point in entry.get("points") or []:
            merged.setdefault(point.get("knowledge_point_id"), point)
    return [item for key, item in merged.items() if key]


def _aggregate(
    db, attempt: Attempt, entries: list[dict], coverage: dict
) -> dict:
    statuses = [entry["status"] for entry in entries]
    if any(status == "pending" for status in statuses):
        overall = "pending"
    elif statuses and all(status == "ready" for status in statuses):
        overall = "ready"
    elif any(status == "ready" for status in statuses):
        overall = "partial"
    elif any(status == "failed" for status in statuses):
        overall = "failed"
    else:
        overall = "missing"

    ready = sum(1 for status in statuses if status == "ready")
    pending = sum(1 for status in statuses if status == "pending")
    failed = sum(1 for status in statuses if status == "failed")
    missing = sum(1 for status in statuses if status == "missing")
    parts = [f"{len(entries)} 份资料", f"{ready} 份已完成"]
    if pending:
        parts.append(f"{pending} 份进行中")
    if failed:
        parts.append(f"{failed} 份待重试")
    if missing:
        parts.append(f"{missing} 份待生成")
    label = " · ".join(parts)
    retry_sources = [
        entry["source_id"]
        for entry in entries
        if entry["status"] in {"failed", "missing"}
    ]

    if len(entries) == 1 and entries[0]["snapshot_id"]:
        snapshot = db.get(LearnerProfileSnapshot, entries[0]["snapshot_id"]) 
        payload = _snapshot_payload(db, snapshot) if snapshot is not None else {}
        payload.update(
            {
                "attempt_id": attempt.id,
                "multi_source": False,
                "label": label,
                "sources": entries,
                "coverage": coverage,
                "retry_sources": retry_sources,
            }
        )
        return payload

    findings: list[dict] = []
    route_steps: list[dict] = []
    unknowns: list[dict] = []
    evidence: list[dict] = []
    seen_evidence: set[str] = set()
    for entry in entries:
        findings.extend(entry.get("findings") or [])
        route_steps.extend(entry.get("route_steps") or [])
        unknowns.extend(entry.get("unknowns") or [])
        for item in entry.get("evidence") or []:
            if item.get("evidence_id") in seen_evidence:
                continue
            seen_evidence.add(item.get("evidence_id"))
            evidence.append(item)

    completed = [entry["completed_at"] for entry in entries if entry.get("completed_at")]
    failure = None
    if overall == "failed":
        failures = [entry["failure"] for entry in entries if entry.get("failure")]
        if failures:
            failure = failures[0]
    return {
        "id": None,
        "attempt_id": attempt.id,
        "multi_source": len(entries) > 1,
        "status": overall,
        "label": label,
        "rule_version": PROFILE_RULE_VERSION,
        "sources": entries,
        "coverage": coverage,
        "findings": findings,
        "route_steps": route_steps,
        "unknowns": unknowns,
        "points": _merge_points(entries),
        "evidence": evidence,
        "diagnosis": [],
        "self_assessment": [],
        "completed_at": max(completed) if completed else None,
        "retry_sources": retry_sources,
        "failure": failure,
    }


def snapshots_for_attempt(
    db, attempt_id: str, *, status: str | None = None
) -> list[LearnerProfileSnapshot]:
    query = select(LearnerProfileSnapshot).where(
        LearnerProfileSnapshot.attempt_id == attempt_id
    )
    if status is not None:
        query = query.where(LearnerProfileSnapshot.status == status)
    return list(db.execute(query.order_by(LearnerProfileSnapshot.created_at.desc())).scalars().all())


def generate_for_attempt(
    db,
    attempt: Attempt,
    *,
    force: bool = False,
    client=None,
    source_id: str | None = None,
) -> dict:
    _, inputs, coverage = _build_all_inputs(db, attempt)
    if source_id and not any(
        item["source"]["source_id"] == source_id for item in inputs
    ):
        raise AssessmentError(
            "assessment_source_not_in_attempt",
            "本次答卷在这份资料上没有可核对的作答证据",
            422,
        )
    entries: list[dict] = []
    for input_data in inputs:
        current_source = input_data["source"]["source_id"]
        snapshot = _latest_snapshot_for_source(db, attempt.id, current_source)
        if source_id and current_source != source_id:
            entries.append(_source_entry(db, input_data, snapshot, coverage))
            continue
        reusable = None if force else _reusable_snapshot(db, attempt, input_data)
        if reusable is not None:
            entries.append(_source_entry(db, input_data, reusable, coverage))
            continue
        payload = _generate_source(db, attempt, input_data, client)
        if payload.get("id"):
            snapshot = db.get(LearnerProfileSnapshot, payload["id"])
            entries.append(_source_entry(db, input_data, snapshot, coverage))
        else:
            entries.append(
                _source_entry(
                    db,
                    input_data,
                    None,
                    coverage,
                    failure={
                        "retryable": payload.get("retryable", True),
                        "failure": payload.get("failure"),
                    },
                )
            )
    return _aggregate(db, attempt, entries, coverage)


def aggregate_for_attempt(db, attempt: Attempt) -> dict | None:
    try:
        _, inputs, coverage = _build_all_inputs(db, attempt)
    except AssessmentError:
        return None
    entries = [
        _source_entry(
            db,
            input_data,
            _latest_snapshot_for_source(db, attempt.id, input_data["source"]["source_id"]),
            coverage,
        )
        for input_data in inputs
    ]
    if not any(entry["snapshot_id"] for entry in entries):
        return None
    return _aggregate(db, attempt, entries, coverage)


def _failure_payload(db, snapshot, code: str, message: str, *, retryable: bool) -> dict:
    if snapshot is not None:
        db.rollback()
        snapshot = db.get(LearnerProfileSnapshot, snapshot.id)
        if snapshot is not None:
            snapshot.status = "failed"
            snapshot.failure_code = code
            snapshot.failure_message = message
            snapshot.retryable = retryable
            snapshot.completed_at = now_iso()
            db.commit()
            return _snapshot_payload(db, snapshot)
    return {
        "id": None,
        "status": "failed",
        "retryable": retryable,
        "failure": {"code": code, "message": message},
    }


def snapshot_for_plan(db, source_id: str, page_start=None, page_end=None) -> dict | None:
    snapshot = latest_effective(db, source_id, page_start, page_end)
    return _snapshot_payload(db, snapshot) if snapshot is not None else None


def snapshot_payload(db, snapshot: LearnerProfileSnapshot) -> dict:
    return _snapshot_payload(db, snapshot)


def snapshot_by_id(db, snapshot_id: str | None) -> dict | None:
    if not snapshot_id:
        return None
    snapshot = db.get(LearnerProfileSnapshot, snapshot_id)
    return _snapshot_payload(db, snapshot) if snapshot is not None else None


def profile_payload(db, source_id: str | None = None) -> dict:
    query = select(LearnerProfileSnapshot).where(LearnerProfileSnapshot.status == "ready")
    if source_id:
        query = query.where(LearnerProfileSnapshot.source_id == source_id)
    rows = db.execute(
        query.order_by(
            LearnerProfileSnapshot.completed_at.desc(),
            LearnerProfileSnapshot.created_at.desc(),
        )
    ).scalars().all()
    latest_by_source: dict[str, LearnerProfileSnapshot] = {}
    for row in rows:
        latest_by_source.setdefault(row.source_id, row)
    items = []
    for source_id_value, snapshot in latest_by_source.items():
        payload = _snapshot_payload(db, snapshot)
        source = db.get(SourceMaterial, source_id_value)
        history = db.execute(
            select(LearnerProfileSnapshot.id, LearnerProfileSnapshot.attempt_id, LearnerProfileSnapshot.completed_at)
            .where(
                LearnerProfileSnapshot.source_id == source_id_value,
                LearnerProfileSnapshot.status == "ready",
            )
            .order_by(LearnerProfileSnapshot.completed_at.desc())
            .limit(5)
        ).all()
        payload["source_name"] = source.display_name if source is not None else "资料已删除"
        payload["history"] = [
            {"id": item[0], "attempt_id": item[1], "completed_at": item[2]} for item in history
        ]
        items.append(payload)
    return {
        "learner_key": LEARNER_KEY,
        "rule_version": PROFILE_RULE_VERSION,
        "items": items,
        "empty": not items,
    }


def question_links_for_payload(payload: dict) -> list[dict]:
    return [
        {
            "question_id": item["question_id"],
            "pack_id": item["pack_id"],
            "attempt_id": item["attempt_id"],
            "citation_id": (item.get("citation") or {}).get("citation_id"),
        }
        for item in payload.get("evidence") or []
    ]