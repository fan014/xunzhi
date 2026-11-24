from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import mastery
from app.domain.knowledge import is_fragment_point, normalize_point_name
from app.domain.models import (
    Citation,
    KnowledgePoint,
    LearnerProfileSnapshot,
    LearningEvidence,
    LearningPlan,
    LearningPlanVersion,
    SourceMaterial,
    now_iso,
)
from app.infra.ids import new_id

PLAN_RULE_VERSION = "plan_v2_modules"
META_POINT_MARKERS = (
    "学习规划",
    "整体结构",
    "学习顺序",
    "学习安排",
    "学习目标",
    "整体学习",
    "学习计划",
    "学习路径",
    "模块划分",
    "模块学习",
    "路径规划",
    "学习步骤",
    "全部学习",
    "从头学",
    "学完整门课",
    "整门课",
    "完整学习",
    "全部内容",
    "全部资料",
    "整库学习",
    "学完这门课",
    "完整课程",
    "整门课程",
    "全部章节",
    "所有章节",
    "全部知识点",
    "整本书",
)
STATE_PRIORITY = {"needs_reinforce": 3, "consolidating": 2, "pending": 1, "stable": 0}
STRENGTH_ORDER = {"sufficient": 3, "moderate": 2, "weak": 1}
HANDOUT_FOCUS = {
    "needs_reinforce": ["原文回顾", "易错情形对照", "同考点新题"],
    "consolidating": ["不同情境验证题"],
    "pending": ["简短判断题补测"],
    "stable": ["少量回顾"],
}


def _sources(payload) -> list[dict]:
    return [
        {
            "source_id": item.source_id,
            "page_start": item.page_start,
            "page_end": item.page_end,
        }
        for item in payload.sources
    ]


def _scope(payload) -> dict:
    return {
        "learned": list(payload.learned),
        "unlearned": list(payload.unlearned),
        "goal": (payload.goal or "").strip(),
        "unlearned_point_ids": list(getattr(payload, "unlearned_point_ids", []) or []),
        "learning_mode": getattr(payload, "learning_mode", "auto") or "auto",
    }


def _items(payload) -> list[dict]:
    return [
        {
            "knowledge_point_id": item.knowledge_point_id,
            "display_name": item.display_name,
            "priority": item.priority,
            "suggested_question_count": item.suggested_question_count,
            "basis": [basis.model_dump() for basis in item.basis],
        }
        for item in payload.points
    ]


def _version_payload(version: LearningPlanVersion) -> dict:
    return {
        "id": version.id,
        "plan_id": version.plan_id,
        "previous_version_id": version.previous_version_id,
        "status": version.status,
        "origin": version.origin,
        "base_version_id": version.base_version_id,
        "suggestion_key": version.suggestion_key,
        "evidence_fingerprint": version.evidence_fingerprint,
        "input_hash": version.input_hash,
        "adjustment": json.loads(version.adjustment_json or "[]"),
        "sources": json.loads(version.sources_json or "[]"),
        "scope": json.loads(version.scope_json or "{}"),
        "items": json.loads(version.items_json or "[]"),
        "modules": json.loads(version.modules_json or "[]"),
        "question_count": version.question_count,
        "handout_advice": version.handout_advice,
        "assessment_id": version.assessment_id,
        "created_at": version.created_at,
        "confirmed_at": version.confirmed_at,
    }


def plan_payload(db: Session, plan: LearningPlan) -> dict:
    versions = db.execute(
        select(LearningPlanVersion)
        .where(LearningPlanVersion.plan_id == plan.id)
        .order_by(LearningPlanVersion.created_at, LearningPlanVersion.id)
    ).scalars().all()
    current = db.get(LearningPlanVersion, plan.current_version_id) if plan.current_version_id else None
    return {
        "id": plan.id,
        "title": plan.title,
        "status": plan.status,
        "current_version_id": plan.current_version_id,
        "primary_source_id": plan.primary_source_id,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
        "current": _version_payload(current) if current is not None else None,
        "versions": [_version_payload(version) for version in versions],
    }


def create_plan(db: Session, payload) -> dict:
    sources = _sources(payload)
    plan = LearningPlan(
        id=new_id("plan"),
        title=(payload.title or "").strip() or "学习计划",
        status="draft",
        primary_source_id=sources[0]["source_id"] if sources else None,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    db.add(plan)
    db.flush()
    version = LearningPlanVersion(
        id=new_id("plan_version"),
        plan_id=plan.id,
        previous_version_id=None,
        status="draft",
        origin="manual",
        sources_json=json.dumps(sources, ensure_ascii=False),
        scope_json=json.dumps(_scope(payload), ensure_ascii=False),
        items_json=json.dumps(_items(payload), ensure_ascii=False),
        adjustment_json="[]",
        question_count=payload.question_count,
        handout_advice=payload.handout_advice or "",
        created_at=now_iso(),
    )
    db.add(version)
    db.flush()
    plan.current_version_id = version.id
    db.commit()
    return plan_payload(db, plan)


def add_version(db: Session, plan: LearningPlan, payload) -> dict:
    previous = db.get(LearningPlanVersion, plan.current_version_id) if plan.current_version_id else None
    version = LearningPlanVersion(
        id=new_id("plan_version"),
        plan_id=plan.id,
        previous_version_id=previous.id if previous is not None else None,
        status="draft",
        origin="manual",
        sources_json=json.dumps(_sources(payload), ensure_ascii=False),
        scope_json=json.dumps(_scope(payload), ensure_ascii=False),
        items_json=json.dumps(_items(payload), ensure_ascii=False),
        adjustment_json="[]",
        question_count=payload.question_count,
        handout_advice=payload.handout_advice or "",
        created_at=now_iso(),
    )
    db.add(version)
    db.flush()
    plan.current_version_id = version.id
    plan.status = "draft"
    plan.updated_at = now_iso()
    db.commit()
    return plan_payload(db, plan)


def confirmation_missing(db: Session, version: LearningPlanVersion) -> list[str]:
    missing: list[str] = []
    sources = json.loads(version.sources_json or "[]")
    if not sources:
        missing.append("sources")
    for index, spec in enumerate(sources):
        source = db.get(SourceMaterial, spec.get("source_id"))
        if source is None or source.status != "active":
            missing.append(f"sources[{index}].source_id")
            continue
        start = spec.get("page_start")
        end = spec.get("page_end")
        if start is not None and end is not None and start > end:
            missing.append(f"sources[{index}].page_range")
    scope = json.loads(version.scope_json or "{}")
    if not (scope.get("goal") or "").strip():
        missing.append("goal")
    return missing


def validate_allocation(version: LearningPlanVersion) -> list[str]:
    items = json.loads(version.items_json or "[]")
    if not items:
        return []
    problems: list[str] = []
    total = version.question_count or 0
    seen = set()
    for index, item in enumerate(items):
        point_id = item.get("knowledge_point_id")
        if point_id in seen:
            problems.append(f"items[{index}].knowledge_point_id")
        if point_id:
            seen.add(point_id)
        count = item.get("suggested_question_count", 0)
        if not isinstance(count, int) or count < 0:
            problems.append(f"items[{index}].suggested_question_count")
    counts = sum(item.get("suggested_question_count", 0) or 0 for item in items)
    if total and counts != total:
        problems.append("items.question_count_sum")
    modules = json.loads(version.modules_json or "[]")
    if modules:
        module_ids = set()
        module_counts = 0
        for index, module in enumerate(modules):
            module_id = module.get("module_id")
            if not module_id or module_id in module_ids:
                problems.append(f"modules[{index}].module_id")
            module_ids.add(module_id)
            count = module.get("suggested_question_count", 0)
            if not isinstance(count, int) or count < 0:
                problems.append(f"modules[{index}].suggested_question_count")
            module_counts += count or 0
            for point_id in module.get("knowledge_point_ids") or []:
                if point_id not in seen:
                    problems.append(f"modules[{index}].knowledge_point_ids")
        if total and module_counts != total:
            problems.append("modules.question_count_sum")
    return problems


def _mark_version_confirmed(
    plan: LearningPlan, version: LearningPlanVersion, *, update_current: bool = True
) -> None:
    version.status = "confirmed"
    version.confirmed_at = now_iso()
    plan.status = "confirmed"
    plan.updated_at = now_iso()
    if update_current:
        plan.current_version_id = version.id


def confirm_version(db: Session, plan: LearningPlan, version: LearningPlanVersion) -> dict:
    _mark_version_confirmed(plan, version)
    db.commit()
    return plan_payload(db, plan)


def _key(spec: dict) -> tuple:
    return (spec.get("source_id"), spec.get("page_start"), spec.get("page_end"))


def sources_match(version_sources: list[dict], request_sources: list[dict]) -> bool:
    return {_key(item) for item in version_sources} == {_key(item) for item in request_sources}


def _sha(payload) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_scope(source_id: str, page_start: int | None, page_end: int | None) -> dict:
    return {"source_id": source_id, "page_start": page_start, "page_end": page_end}


def _evidence_fingerprint(
    db: Session, source_id: str, page_start=None, page_end=None
) -> tuple[str, int]:
    query = select(LearningEvidence).where(
        LearningEvidence.source_id == source_id,
        LearningEvidence.reliability == "confirmed",
        LearningEvidence.knowledge_point_id.is_not(None),
        LearningEvidence.source_type.in_(("practice", "retry")),
    )
    rows = db.execute(query).scalars().all()
    included = []
    excluded_ids: set[str] = set()
    for row in rows:
        if page_start is not None and page_end is not None:
            citation = db.get(Citation, row.citation_id) if row.citation_id else None
            if (
                citation is None
                or citation.page_no is None
                or not (page_start <= citation.page_no <= page_end)
            ):
                excluded_ids.add(row.id)
                continue
        included.append(
            [
                row.id,
                row.attempt_id,
                row.question_id,
                row.result,
                row.submitted_at or "",
                row.knowledge_point_id,
            ]
        )
    included.sort()
    return (
        _sha({"rows": included, "excluded_count": len(excluded_ids)}),
        len(excluded_ids),
    )


def _user_input_hash(payload) -> str:
    return _sha(
        {
            "goal": (payload.goal or "").strip(),
            "learned": list(payload.learned),
            "unlearned": list(payload.unlearned),
            "unlearned_point_ids": list(getattr(payload, "unlearned_point_ids", []) or []),
            "question_count": payload.question_count,
            "learning_mode": getattr(payload, "learning_mode", "auto"),
            "page_start": getattr(payload, "page_start", None),
            "page_end": getattr(payload, "page_end", None),
            "handout_advice": getattr(payload, "handout_advice", "") or "",
        }
    )


def _suggestion_key(source_scope, previous_version_id, evidence_fingerprint, input_hash, assessment_id=None) -> str:
    return _sha(
        {
            "rule": PLAN_RULE_VERSION,
            "source": source_scope,
            "previous": previous_version_id,
            "evidence": evidence_fingerprint,
            "input": input_hash,
            "assessment": assessment_id,
        }
    )


def latest_confirmed_version(db: Session, source_id: str) -> LearningPlanVersion | None:
    plans = db.execute(
        select(LearningPlan)
        .where(LearningPlan.status == "confirmed")
        .order_by(LearningPlan.updated_at.desc())
    ).scalars().all()
    for plan in plans:
        version = db.get(LearningPlanVersion, plan.current_version_id) if plan.current_version_id else None
        if version is None or version.status != "confirmed":
            continue
        if plan.primary_source_id == source_id:
            return version
        sources = json.loads(version.sources_json or "[]")
        if any(spec.get("source_id") == source_id for spec in sources if isinstance(spec, dict)):
            return version
    return None


def resolve_previous_version(db: Session, source_id: str, previous_version_id: str | None):
    if not previous_version_id:
        return latest_confirmed_version(db, source_id)
    version = db.get(LearningPlanVersion, previous_version_id)
    if version is None or version.status != "confirmed":
        raise ValueError("previous_version_mismatch")
    sources = json.loads(version.sources_json or "[]")
    if not any(spec.get("source_id") == source_id for spec in sources if isinstance(spec, dict)):
        raise ValueError("previous_version_mismatch")
    return version


def _assessment_index(assessment: dict | None) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    if not assessment or assessment.get("status") != "ready":
        return {}, {}
    findings: dict[str, list[dict]] = {}
    routes: dict[str, list[dict]] = {}
    for item in assessment.get("findings") or []:
        if not isinstance(item, dict):
            continue
        findings.setdefault(item.get("knowledge_point_id"), []).append(item)
    for item in assessment.get("route_steps") or []:
        if not isinstance(item, dict):
            continue
        routes.setdefault(item.get("knowledge_point_id"), []).append(item)
    return findings, routes


def _rank_points(points, mastery_by_point, previous_items, goal_ids, excluded_ids, assessment=None):
    previous_by_id = {item.get("knowledge_point_id"): item for item in previous_items}
    findings_by_point, routes_by_point = _assessment_index(assessment)
    ranked = []
    for point in points:
        if point.id in excluded_ids:
            continue
        result = mastery_by_point[point.id]
        previous = previous_by_id.get(point.id) or {}
        findings = findings_by_point.get(point.id) or []
        routes = routes_by_point.get(point.id) or []
        ranked.append(
            {
                "point": point,
                "state": result.state,
                "mastery": result,
                "is_goal": point.id in goal_ids,
                "previous": previous,
                "assessment_findings": findings,
                "assessment_routes": routes,
                "assessment_focus": bool(findings or routes),
            }
        )
    ranked.sort(
        key=lambda item: (
            0 if item["is_goal"] else 1,
            0 if item["assessment_focus"] else 1,
            0 if item["state"] == "needs_reinforce" else 1,
            -STATE_PRIORITY.get(item["state"], 0),
            -STRENGTH_ORDER.get(item["mastery"].evidence_strength, 0),
            -(item["previous"].get("suggested_question_count") or 0),
            item["point"].id,
        )
    )
    for item in ranked:
        item["rank_basis"] = {
            "is_goal": item["is_goal"],
            "state": item["state"],
            "evidence_strength": item["mastery"].evidence_strength,
            "previous_question_count": item["previous"].get("suggested_question_count") or 0,
            "assessment_focus": item["assessment_focus"],
        }
    return ranked


AUTO_BASE_COUNTS = {
    "auto": {"needs_reinforce": 3, "consolidating": 2, "pending": 2, "stable": 1},
    "foundation": {"needs_reinforce": 3, "consolidating": 2, "pending": 2, "stable": 1},
    "focus": {"needs_reinforce": 3, "consolidating": 2, "pending": 1, "stable": 1},
}


def _base_count(state: str, is_goal: bool, mode: str = "explicit") -> int:
    if mode in AUTO_BASE_COUNTS:
        return AUTO_BASE_COUNTS[mode].get(state, 1)
    if state == "needs_reinforce":
        return 2
    if state == "consolidating":
        return 1
    if state == "pending":
        return 1 if is_goal else 0
    return 1 if is_goal else 0


def _interleave_by_source(ranked: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in ranked:
        source_id = item["point"].source_id or ""
        if source_id not in groups:
            groups[source_id] = []
            order.append(source_id)
        groups[source_id].append(item)
    result: list[dict] = []
    while any(groups[source_id] for source_id in order):
        for source_id in order:
            if groups[source_id]:
                result.append(groups[source_id].pop(0))
    return result


def _allocate(
    ranked, total: int, goal_ids: set[str], mode: str = "explicit"
) -> tuple[list[dict], list[dict]]:
    auto = mode in AUTO_BASE_COUNTS
    for item in ranked:
        item["count"] = _base_count(item["state"], item["is_goal"], mode)
        if auto:
            continue
        if item.get("assessment_focus"):
            actions = {step.get("action") for step in item.get("assessment_routes") or []}
            if item["state"] == "needs_reinforce":
                desired = 2 if actions & {"new_question", "retest", "compare_explanation"} else 1
            else:
                desired = 1
            item["count"] = max(item["count"], desired)
    if auto:
        items = [item for item in ranked if item["count"] > 0]
        return items, []
    counts = sum(item["count"] for item in ranked)
    order_cut = ["stable", "pending", "consolidating", "needs_reinforce"]
    while counts > total:
        changed = False
        for state in order_cut:
            for item in reversed(ranked):
                if item["state"] != state or item["count"] <= 0:
                    continue
                if state == "needs_reinforce" and item["count"] <= 1:
                    continue
                if item.get("assessment_focus") and any(
                    step.get("action") in {"new_question", "retest", "compare_explanation"}
                    for step in item.get("assessment_routes") or []
                ) and item["count"] <= 1:
                    continue
                item["count"] -= 1
                counts -= 1
                changed = True
                break
            if changed:
                break
        if not changed:
            for item in reversed(ranked):
                if item["count"] > 0 and not item["is_goal"] and not item.get("assessment_focus"):
                    item["count"] -= 1
                    counts -= 1
                    changed = True
                    break
        if not changed:
            for item in reversed(ranked):
                if item["count"] > 0 and not item["is_goal"]:
                    item["count"] -= 1
                    counts -= 1
                    changed = True
                    break
        if not changed:
            break
    while counts < total:
        changed = False
        for item in ranked:
            if item["is_goal"] or item["state"] == "needs_reinforce":
                item["count"] += 1
                counts += 1
                changed = True
                break
        if not changed:
            for item in reversed(ranked):
                if item["state"] == "needs_reinforce":
                    continue
                if any(
                    finding.get("kind") == "possible_misunderstanding"
                    for finding in item.get("assessment_findings") or []
                ):
                    continue
                if not item.get("assessment_focus") and not item["is_goal"]:
                    continue
                item["count"] += 1
                counts += 1
                changed = True
                break
        if not changed:
            ranked[0]["count"] += 1
            counts += 1
        elif not ranked:
            break
    items = [item for item in ranked if item["count"] > 0]
    unplaced = [
        {
            "knowledge_point_id": item["point"].id,
            "display_name": item["point"].display_name,
            "reason": "本轮题量不足，未安排。",
        }
        for item in ranked
        if item["count"] <= 0
    ]
    return items, unplaced


def _build_adjustment(item) -> dict:
    state = item["state"]
    result = item["mastery"]
    previous = item["previous"]
    previous_count = previous.get("suggested_question_count") or 0
    refs = result.review_targets or []
    findings = item.get("assessment_findings") or []
    routes = item.get("assessment_routes") or []
    if state == "needs_reinforce":
        reason = result.decision_reason
    elif state == "consolidating" and "corrected_pending_new" in result.flags:
        reason = "上次只做对了原题：已订正，安排不同情境的新题验证。"
    elif state == "consolidating":
        reason = result.decision_reason
    elif state == "pending":
        reason = "目前没有可靠作答；如与本次目标相关，用一道简短判断题补测。"
    else:
        reason = "最近两次都用不同题答对，减少重复讲解，只保留少量回顾。"
    if findings:
        detail = findings[0].get("judgement") or ""
        if detail:
            reason = f"{reason} 个人档案：{detail}"
    adjustment = {
        "knowledge_point_id": item["point"].id,
        "display_name": item["point"].display_name,
        "previous_question_count": previous_count,
        "current_question_count": item["count"],
        "previous_priority": previous.get("priority") or "new",
        "current_priority": state,
        "reason": reason,
        "evidence_refs": refs[:3],
        "assessment": {
            "finding_kind": findings[0].get("kind") if findings else None,
            "judgement": findings[0].get("judgement") if findings else "",
            "evidence_ids": findings[0].get("evidence_ids") if findings else [],
            "route_action": routes[0].get("action") if routes else None,
            "route_action_label": routes[0].get("action_label") if routes else "",
            "route_instruction": routes[0].get("instruction") if routes else "",
            "route_completion": routes[0].get("completion") if routes else "",
        },
    }
    return adjustment


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _module_id(point_ids: list[str]) -> str:
    digest = hashlib.sha1("|".join(sorted(point_ids)).encode("utf-8")).hexdigest()[:12]
    return f"mod_{digest}"


def _module_rationale(items: list[dict], mode: str, position: int) -> str:
    first = items[0]
    findings = [
        finding
        for item in items
        for finding in (item.get("assessment_findings") or [])
    ]
    if findings:
        reason = f"个人档案：{findings[0].get('judgement') or '最近练习暴露了需要继续验证的地方。'}"
    elif any(item["state"] == "needs_reinforce" for item in items):
        reason = "最近一次练习出现了答错，先补薄弱点。"
    elif all(item["state"] == "stable" for item in items):
        reason = "最近表现较稳，安排简短回顾和迁移题，避免重复堆题。"
    elif any(item["state"] == "consolidating" for item in items):
        reason = "已经答对过，还需要在不同情境里验证是否真正掌握。"
    elif any(item["state"] == "pending" for item in items):
        reason = "这部分还没有可靠作答，先用讲解和新题建立基线。"
    else:
        reason = "依据资料顺序和诊断确认的学习范围安排。"
    if position == 0 and any(item["state"] == "needs_reinforce" for item in items):
        reason = f"优先安排：{reason}"
    if mode == "foundation":
        reason = f"{reason} 从基础学：先讲清概念，再进入应用。"
    elif mode == "focus":
        reason = f"{reason} 重点复习：压缩已掌握内容，把题量留给薄弱点。"
    count = sum(item["count"] for item in items)
    if count >= 4:
        reason = f"{reason} 本模块多安排 1–2 道题，因为覆盖多个考点且需要验证迁移。"
    return reason


_NOISE_TITLE_RE = re.compile(
    r"^(?:\d+/\d+|第\s*\d+\s*[-–—]\s*\d+\s*页|.*\.(?:pdf|txt|md|png|jpe?g|webp))$",
    re.IGNORECASE,
)
_CIDR_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}$")


def _module_title(names: list[str], index: int) -> str:
    clean = [name.strip() for name in names if name and not _NOISE_TITLE_RE.match(name.strip())]
    if not clean:
        return f"核心概念 {index}"
    if len(clean) == 1 and _CIDR_RE.match(clean[0]):
        return f"如何理解 {clean[0]}"
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]}、{clean[1]}"
    return f"{clean[0]}等{len(clean)}个概念"


def _build_modules(
    items: list[dict], *, mode: str, source_scope: dict, total: int, source_names: dict | None = None
) -> list[dict]:
    if not items:
        return []
    chunks = [items] if len(items) <= 3 else [items[index : index + 2] for index in range(0, len(items), 2)]
    modules = []
    for index, chunk in enumerate(chunks, start=1):
        names = [item["point"].display_name for item in chunk]
        point_ids = [item["point"].id for item in chunk]
        source_ids = sorted({item["point"].source_id for item in chunk if item["point"].source_id})
        count = sum(item["count"] for item in chunk)
        starts = [item["point"].page_start for item in chunk if item["point"].page_start is not None]
        ends = [item["point"].page_end for item in chunk if item["point"].page_end is not None]
        page_start = min(starts) if starts else source_scope.get("page_start")
        page_end = max(ends) if ends else source_scope.get("page_end")
        source_scopes = []
        for source_id in source_ids:
            rows = [item["point"] for item in chunk if item["point"].source_id == source_id]
            row_starts = [row.page_start for row in rows if row.page_start is not None]
            row_ends = [row.page_end for row in rows if row.page_end is not None]
            source_scopes.append(
                {
                    "source_id": source_id,
                    "source_name": (source_names or {}).get(source_id, source_id),
                    "page_start": min(row_starts) if row_starts else None,
                    "page_end": max(row_ends) if row_ends else None,
                }
            )
        title = _module_title(names, index)
        strong = [item for item in chunk if item["state"] == "needs_reinforce"]
        priority = "needs_reinforce" if strong else (
            "consolidating" if any(item["state"] == "consolidating" for item in chunk)
            else ("pending" if any(item["state"] == "pending" for item in chunk) else "stable")
        )
        evidence_ids = sorted(
            {
                evidence_id
                for item in chunk
                for finding in (item.get("assessment_findings") or [])
                for evidence_id in (finding.get("evidence_ids") or [])
            }
        )
        review_targets = [
            {
                "question_id": ref.get("question_id"),
                "evidence_id": ref.get("evidence_id"),
                "citation_id": ref.get("citation_id"),
            }
            for item in chunk
            for ref in (item["mastery"].review_targets or [])
            if ref.get("question_id")
        ]
        if mode == "foundation":
            completion = "能用自己的话解释本模块的核心概念，并完成 2 道基础应用题。"
        elif mode == "focus":
            completion = "能在不同情境中答对薄弱考点；已掌握内容能用一句话回顾。"
        else:
            completion = "能在不同情境中答对主要考点，并说清判断依据。"
        modules.append(
            {
                "module_id": _module_id(point_ids),
                "ordinal": index,
                "title": _clip(title, 36),
                "objective": _clip(
                    "掌握"
                    + "、".join(names)
                    + ("的应用与判断依据。" if mode != "foundation" else "的基本概念与应用。"),
                    120,
                ),
                "core_points": names,
                "knowledge_point_ids": point_ids,
                "completion_standard": completion,
                "rationale": _module_rationale(chunk, mode, index - 1),
                "priority": priority,
                "learning_mode": mode,
                "suggested_question_count": count,
                "source_id": source_ids[0] if len(source_ids) == 1 else source_scope.get("source_id"),
                "source_ids": source_ids,
                "source_names": [
                    (source_names or {}).get(source_id, source_id)
                    for source_id in source_ids
                ],
                "source_scopes": source_scopes,
                "page_start": page_start,
                "page_end": page_end,
                "basis": {
                    "point_ids": point_ids,
                    "evidence_ids": evidence_ids,
                    "review_targets": review_targets[:4],
                },
            }
        )
    if total and sum(module["suggested_question_count"] for module in modules) != total:
        raise ValueError("plan_module_allocation_invalid")
    return modules


def refresh_module_counts(version: LearningPlanVersion) -> None:
    items = json.loads(version.items_json or "[]")
    counts = {item.get("knowledge_point_id"): int(item.get("suggested_question_count") or 0) for item in items}
    modules = json.loads(version.modules_json or "[]")
    if not modules:
        return
    for module in modules:
        module["suggested_question_count"] = sum(
            counts.get(point_id, 0) for point_id in module.get("knowledge_point_ids") or []
        )
    version.modules_json = json.dumps(modules, ensure_ascii=False)
    version.question_count = sum(counts.values())


def _point_candidates(db: Session, source_id: str):
    rows = db.execute(
        select(KnowledgePoint)
        .where(KnowledgePoint.source_id == source_id, KnowledgePoint.status == "active")
        .order_by(KnowledgePoint.id)
    ).scalars().all()
    return [
        point
        for point in rows
        if not any(marker in (point.display_name or "") for marker in META_POINT_MARKERS)
        and not is_fragment_point(point.display_name or "")
    ]


def suggest_next_plan(db: Session, source_id: str, payload, assessment: dict | None = None) -> dict:
    source = db.get(SourceMaterial, source_id)
    if source is None or source.status != "active":
        raise ValueError("source_not_found")
    previous = resolve_previous_version(db, source_id, getattr(payload, "previous_version_id", None))
    previous_items = json.loads(previous.items_json or "[]") if previous is not None else []
    points = _point_candidates(db, source_id)
    mastery_by_point = {
        point.id: mastery.evaluate_point_mastery(db, point, source_scope=[source_id])
        for point in points
    }
    goal_ids = {
        item.knowledge_point_id
        for item in getattr(payload, "points", [])
        if item.knowledge_point_id
    }
    excluded_ids = set(getattr(payload, "unlearned_point_ids", []) or [])
    for point_id, result in mastery_by_point.items():
        if result.source_deleted:
            excluded_ids.add(point_id)
    ranked = _rank_points(
        points, mastery_by_point, previous_items, goal_ids, excluded_ids, assessment=assessment
    )
    requested_count = getattr(payload, "question_count", None)
    legacy_count = requested_count in {3, 5, 8}
    mode = "explicit" if legacy_count else (getattr(payload, "learning_mode", "auto") or "auto")
    if not legacy_count and mode == "foundation":
        ranked.sort(
            key=lambda item: (
                {"pending": 0, "needs_reinforce": 1, "consolidating": 2, "stable": 3}.get(item["state"], 4),
                item["point"].id,
            )
        )
    elif not legacy_count and mode == "focus":
        ranked.sort(
            key=lambda item: (
                {"needs_reinforce": 0, "consolidating": 1, "pending": 2, "stable": 3}.get(item["state"], 4),
                item["point"].id,
            )
        )
    total = requested_count if legacy_count else 0
    items, unplaced = _allocate(ranked, total, goal_ids, mode="explicit" if legacy_count else mode)
    if not legacy_count:
        total = sum(item["count"] for item in items)
    adjustments = [_build_adjustment(item) for item in items]
    findings_by_point, routes_by_point = _assessment_index(assessment)
    assessment_payload = None
    if assessment and assessment.get("status") == "ready":
        planned_ids = {item["point"].id for item in items}
        assessment_payload = {
            "id": assessment.get("id"),
            "attempt_id": assessment.get("attempt_id"),
            "completed_at": assessment.get("completed_at"),
            "findings": [
                finding
                for point_id in planned_ids
                for finding in findings_by_point.get(point_id, [])
            ],
            "route_steps": [
                route
                for point_id in planned_ids
                for route in routes_by_point.get(point_id, [])
            ],
        }
    assessment_applied = bool(assessment_payload and items and (assessment_payload["findings"] or assessment_payload["route_steps"]))

    source_scope = _source_scope(source_id, getattr(payload, "page_start", None), getattr(payload, "page_end", None))
    modules = (
        []
        if legacy_count
        else _build_modules(
            items,
            mode=mode,
            source_scope=source_scope,
            total=total,
        )
    )
    fingerprint, no_page_evidence_count = _evidence_fingerprint(
        db,
        source_id,
        getattr(payload, "page_start", None),
        getattr(payload, "page_end", None),
    )
    input_hash = _user_input_hash(payload)
    assessment_id = assessment.get("id") if assessment and assessment.get("status") == "ready" else None
    key = _suggestion_key(
        source_scope,
        previous.id if previous is not None else None,
        fingerprint,
        input_hash,
        assessment_id,
    )
    existing = db.execute(
        select(LearningPlanVersion).where(LearningPlanVersion.suggestion_key == key)
    ).scalars().first()
    if existing is not None:
        plan = db.get(LearningPlan, existing.plan_id)
        return {
            "plan": plan_payload(db, plan),
            "version": _version_payload(existing),
            "reused": True,
            "has_update_suggestion": bool(
                previous is not None and previous.evidence_fingerprint != fingerprint
            ),
            "missing": confirmation_missing(db, existing),
            "conflicts": [],
            "no_page_evidence_count": no_page_evidence_count,
            "assessment": assessment_payload,
            "assessment_applied": assessment_applied,
            "assessment_basis": "profile" if assessment_applied else "rules",
            "adjustments": json.loads(existing.adjustment_json or "[]"),
            "modules": json.loads(existing.modules_json or "[]"),
        }

    missing = []
    if not (payload.goal or "").strip():
        missing.append("goal")
    if not source_scope.get("source_id"):
        missing.append("sources")

    plan = db.get(LearningPlan, previous.plan_id) if previous is not None else None
    if plan is None:
        plan = LearningPlan(
            id=new_id("plan"),
            title=(payload.title or source.display_name + " 复习计划").strip(),
            status="draft",
            primary_source_id=source_id,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        db.add(plan)
        db.flush()

    scope_payload = _scope(payload)
    scope_payload["learning_mode"] = mode
    scope_payload["auto_plan"] = not legacy_count
    version = LearningPlanVersion(
        id=new_id("plan_version"),
        plan_id=plan.id,
        previous_version_id=previous.id if previous is not None else None,
        status="draft",
        origin="suggested",
        base_version_id=previous.id if previous is not None else None,
        suggestion_key=key,
        evidence_fingerprint=fingerprint,
        input_hash=input_hash,
        adjustment_json=json.dumps(adjustments, ensure_ascii=False),
        sources_json=json.dumps([source_scope], ensure_ascii=False),
scope_json=json.dumps(scope_payload, ensure_ascii=False),
        modules_json=json.dumps(modules, ensure_ascii=False),
        items_json=json.dumps(
            [
                {
                    "knowledge_point_id": item["point"].id,
                    "display_name": item["point"].display_name,
                    "priority": item["state"],
                    "suggested_question_count": item["count"],
                    "basis": [
                        {
                            "type": "mastery",
                            "state": item["state"],
                            "evidence_strength": item["mastery"].evidence_strength,
                            "rule_version": mastery.MASTERY_RULE_VERSION,
                        }
                    ]
                    + (
                        [
                            {
                                "type": "assessment",
                                "assessment_id": assessment_id,
                                "finding_kind": (item.get("assessment_findings") or [{}])[0].get("kind"),
                                "evidence_ids": (item.get("assessment_findings") or [{}])[0].get("evidence_ids") or [],
                                "route_action": (item.get("assessment_routes") or [{}])[0].get("action"),
                            }
                        ]
                        if assessment_id and item.get("assessment_focus")
                        else []
                    ),
                }
                for item in items
            ],
            ensure_ascii=False,
        ),
        question_count=total,
        handout_advice=payload.handout_advice or "",
        assessment_id=assessment_id,
        created_at=now_iso(),
    )
    db.add(version)
    db.flush()
    plan.current_version_id = version.id
    plan.status = "draft"
    plan.updated_at = now_iso()
    db.commit()
    return {
        "plan": plan_payload(db, plan),
        "version": _version_payload(version),
        "reused": False,
        "has_update_suggestion": bool(
            previous is not None and previous.evidence_fingerprint != fingerprint
        ),
        "missing": missing,
        "conflicts": [],
        "adjustments": adjustments,
        "unplaced_points": unplaced,
        "no_page_evidence_count": no_page_evidence_count,
        "assessment": assessment_payload,
        "assessment_applied": assessment_applied,
        "assessment_basis": "profile" if assessment_applied else "rules",
        "modules": modules,
        "excluded_points": [
            {
                "knowledge_point_id": point_id,
                "reason": "已标记暂未学到或来源不可用",
            }
            for point_id in sorted(excluded_ids)
        ],
    }


def _excluded_meta_points(db: Session, source_specs: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for spec in source_specs:
        source_id = spec.get("source_id")
        if not source_id:
            continue
        points = db.execute(
            select(KnowledgePoint).where(
                KnowledgePoint.source_id == source_id,
                KnowledgePoint.status.in_(("active", "candidate")),
            )
        ).scalars().all()
        for point in points:
            if any(marker in (point.display_name or "") for marker in META_POINT_MARKERS):
                rows.append(
                    {
                        "knowledge_point_id": point.id,
                        "display_name": point.display_name,
                        "source_id": point.source_id,
                        "reason": "学习意图不是具体考点，未纳入计划",
                    }
                )
    return rows


def suggest_kb_plan(
    db: Session,
    source_specs: list[dict],
    payload,
    *,
    knowledge_base_id: str,
    assessment: dict | None = None,
    session_id: str | None = None,
    force_new: bool = False,
) -> dict:
    if not source_specs:
        raise ValueError("plan_sources_missing")
    source_names: dict[str, str] = {}
    points = []
    for spec in source_specs:
        source_id = spec.get("source_id")
        source = db.get(SourceMaterial, source_id)
        if source is None or source.status != "active":
            continue
        source_names[source_id] = spec.get("source_name") or source.display_name
        points.extend(_point_candidates(db, source_id))
    if not points:
        raise ValueError("plan_points_missing")
    excluded_points = _excluded_meta_points(db, source_specs)
    mastery_by_point = {
        point.id: mastery.evaluate_point_mastery(db, point, source_scope=[point.source_id])
        for point in points
    }
    goal_ids = {
        item.knowledge_point_id
        for item in getattr(payload, "points", [])
        if item.knowledge_point_id
    }
    excluded_ids = set(getattr(payload, "unlearned_point_ids", []) or [])
    ranked = _rank_points(points, mastery_by_point, [], goal_ids, excluded_ids, assessment=assessment)
    if len(source_names) > 1:
        ranked = _interleave_by_source(ranked)
    requested_count = getattr(payload, "question_count", None)
    legacy_count = requested_count in {3, 5, 8}
    mode = "explicit" if legacy_count else (getattr(payload, "learning_mode", "auto") or "auto")
    total = requested_count if legacy_count else 0
    items, unplaced = _allocate(ranked, total, goal_ids, mode="explicit" if legacy_count else mode)
    if not legacy_count:
        total = sum(item["count"] for item in items)
    adjustments = [_build_adjustment(item) for item in items]
    modules = _build_modules(
        items,
        mode="auto" if legacy_count else mode,
        source_scope={"source_id": None},
        total=total,
        source_names=source_names,
    )
    findings_by_point, routes_by_point = _assessment_index(assessment)
    assessment_payload = None
    if assessment and assessment.get("status") == "ready":
        planned_ids = {item["point"].id for item in items}
        assessment_payload = {
            "id": assessment.get("id"),
            "attempt_id": assessment.get("attempt_id"),
            "completed_at": assessment.get("completed_at"),
            "findings": [
                finding
                for point_id in planned_ids
                for finding in findings_by_point.get(point_id, [])
            ],
            "route_steps": [
                route
                for point_id in planned_ids
                for route in routes_by_point.get(point_id, [])
            ],
        }
    key = _sha(
        {
            "rule": PLAN_RULE_VERSION,
            "knowledge_base_id": knowledge_base_id,
            "sources": source_specs,
            "goal": (payload.goal or "").strip(),
            "mode": mode,
            "question_count": requested_count,
            "assessment": assessment.get("id") if assessment else None,
            "replan": new_id("plan_version") if force_new else None,
        }
    )
    existing = None
    if not force_new:
        existing = db.execute(
            select(LearningPlanVersion).where(LearningPlanVersion.suggestion_key == key)
        ).scalars().first()
    if existing is not None:
        return {
            "plan": plan_payload(db, db.get(LearningPlan, existing.plan_id)),
            "version": _version_payload(existing),
            "reused": True,
            "modules": json.loads(existing.modules_json or "[]"),
            "adjustments": json.loads(existing.adjustment_json or "[]"),
            "missing": confirmation_missing(db, existing),
            "assessment": assessment_payload,
            "assessment_applied": bool(assessment_payload),
            "assessment_basis": "profile" if assessment_payload else "rules",
            "excluded_points": excluded_points,
            "unplaced_points": unplaced,
        }
    plan = LearningPlan(
        id=new_id("plan"),
        title=(payload.title or f"{source_names.get(source_specs[0]['source_id'], '知识库')} 学习方案").strip(),
        status="draft",
        primary_source_id=source_specs[0].get("source_id"),
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    db.add(plan)
    db.flush()
    scope_payload = _scope(payload)
    scope_payload["learning_mode"] = mode
    scope_payload["auto_plan"] = not legacy_count
    scope_payload["knowledge_base_id"] = knowledge_base_id
    version = LearningPlanVersion(
        id=new_id("plan_version"),
        plan_id=plan.id,
        status="draft",
        origin="suggested",
        suggestion_key=key,
        input_hash=_user_input_hash(payload),
        adjustment_json=json.dumps(adjustments, ensure_ascii=False),
        sources_json=json.dumps(source_specs, ensure_ascii=False),
        scope_json=json.dumps(scope_payload, ensure_ascii=False),
        modules_json=json.dumps(modules, ensure_ascii=False),
        items_json=json.dumps(
            [
                {
                    "knowledge_point_id": item["point"].id,
                    "source_id": item["point"].source_id,
                    "display_name": item["point"].display_name,
                    "priority": item["state"],
                    "suggested_question_count": item["count"],
                    "page_start": item["point"].page_start,
                    "page_end": item["point"].page_end,
                    "basis": [
                        {
                            "type": "mastery",
                            "state": item["state"],
                            "evidence_strength": item["mastery"].evidence_strength,
                            "rule_version": mastery.MASTERY_RULE_VERSION,
                        }
                    ],
                }
                for item in items
            ],
            ensure_ascii=False,
        ),
        diagnostic_session_id=session_id,
        assessment_id=assessment.get("id") if assessment else None,
        question_count=total,
        handout_advice=payload.handout_advice or "",
        created_at=now_iso(),
    )
    db.add(version)
    db.flush()
    plan.current_version_id = version.id
    db.commit()
    return {
        "plan": plan_payload(db, plan),
        "version": _version_payload(version),
        "reused": False,
        "modules": modules,
        "adjustments": adjustments,
        "missing": confirmation_missing(db, version),
        "assessment": assessment_payload,
        "assessment_applied": bool(assessment_payload),
        "assessment_basis": "profile" if assessment_payload else "rules",
        "excluded_points": excluded_points,
        "unplaced_points": unplaced,
    }


def _ambiguous_plan_point_names(constraints: list[dict]) -> bool:
    groups: dict[str, list[dict]] = {}
    for item in constraints:
        name = normalize_point_name(item.get("display_name") or "")
        if name:
            groups.setdefault(name, []).append(item)
    for rows in groups.values():
        if len(rows) <= 1:
            continue
        ids = {row.get("knowledge_point_id") for row in rows}
        sources = {row.get("source_id") for row in rows}
        modules = {row.get("module_id") for row in rows}
        if (
            None in ids
            or len(ids) < len(rows)
            or (len(sources) < 2 and len(modules) < 2)
        ):
            return True
    return False


def build_generation_constraints(db: Session, version: LearningPlanVersion, sources_payload: list[dict]) -> dict:
    if version.status != "confirmed":
        raise ValueError("plan_version_not_confirmed")
    items = json.loads(version.items_json or "[]")
    total = version.question_count or 0
    if items:
        problems = validate_allocation(version)
        if problems:
            raise ValueError("plan_allocation_invalid")
        if total and sum(item.get("suggested_question_count") or 0 for item in items) != total:
            raise ValueError("plan_allocation_invalid")
    scope = json.loads(version.scope_json or "{}")
    excluded_ids = set(scope.get("unlearned_point_ids") or [])
    for item in items:
        if item.get("knowledge_point_id") in excluded_ids:
            raise ValueError("plan_excluded_point")
    planned_modules = json.loads(version.modules_json or "[]")
    auto_plan = bool(scope.get("auto_plan"))
    if auto_plan and (not planned_modules or not items):
        raise ValueError("plan_modules_missing")
    module_by_point = {
        point_id: module
        for module in planned_modules
        for point_id in module.get("knowledge_point_ids") or []
    }
    constraints = []
    planned_ids = {item.get("knowledge_point_id") for item in items}
    assessment_payload = None
    assessment_findings: dict[str, list[dict]] = {}
    assessment_routes: dict[str, list[dict]] = {}
    if version.assessment_id:
        snapshot = db.get(LearnerProfileSnapshot, version.assessment_id)
        if snapshot is not None and snapshot.status == "ready" and snapshot.result_json:
            try:
                data = json.loads(snapshot.result_json)
            except (TypeError, json.JSONDecodeError):
                data = {}
            for finding in data.get("findings") or []:
                if finding.get("knowledge_point_id") in planned_ids:
                    assessment_findings.setdefault(finding["knowledge_point_id"], []).append(finding)
            for route in data.get("route_steps") or []:
                if route.get("knowledge_point_id") in planned_ids:
                    assessment_routes.setdefault(route["knowledge_point_id"], []).append(route)
            assessment_payload = {
                "id": snapshot.id,
                "attempt_id": snapshot.attempt_id,
                "completed_at": snapshot.completed_at,
                "findings": [
                    finding for rows in assessment_findings.values() for finding in rows
                ],
                "route_steps": [
                    route for rows in assessment_routes.values() for route in rows
                ],
            }
    for item in items:
        point_id = item.get("knowledge_point_id")
        point = db.get(KnowledgePoint, point_id) if point_id else None
        findings = assessment_findings.get(point_id) or []
        routes = assessment_routes.get(point_id) or []
        focus = HANDOUT_FOCUS.get(item.get("priority") or "pending", ["简短判断题补测"])
        if routes:
            focus = [route.get("instruction") or label for route, label in (
                (route, route.get("action_label") or "") for route in routes
            )][:3]
        constraints.append(
            {
                "knowledge_point_id": point_id,
                "display_name": item.get("display_name") or "",
                "module_id": (module_by_point.get(point_id) or {}).get("module_id"),
                "module_title": (module_by_point.get(point_id) or {}).get("title"),
                "suggested_question_count": item.get("suggested_question_count") or 0,
                "priority": item.get("priority") or "pending",
                "handout_focus": focus,
                "source_id": item.get("source_id") or (point.source_id if point else None),
                "page_start": item.get("page_start")
                if item.get("page_start") is not None
                else (point.page_start if point else None),
                "page_end": item.get("page_end")
                if item.get("page_end") is not None
                else (point.page_end if point else None),
                "assessment_findings": findings[:2],
                "assessment_routes": routes[:3],
            }
        )
    if _ambiguous_plan_point_names(constraints):
        raise ValueError("plan_point_ambiguous")
    module_constraints = []
    for module in planned_modules:
        point_ids = module.get("knowledge_point_ids") or []
        findings = [
            finding
            for point_id in point_ids
            for finding in assessment_findings.get(point_id, [])
        ]
        routes = [
            route
            for point_id in point_ids
            for route in assessment_routes.get(point_id, [])
        ]
        module_constraints.append(
            {
                **module,
                "assessment_findings": findings[:3],
                "assessment_routes": routes[:4],
                "point_allocation": [
                    {
                        "knowledge_point_id": point_id,
                        "display_name": next(
                            (
                                item.get("display_name")
                                for item in constraints
                                if item.get("knowledge_point_id") == point_id
                            ),
                            "",
                        ),
                        "suggested_question_count": next(
                            (
                                item.get("suggested_question_count")
                                for item in constraints
                                if item.get("knowledge_point_id") == point_id
                            ),
                            0,
                        ),
                    }
                    for point_id in point_ids
                ],
            }
        )
    return {
        "plan_version_id": version.id,
        "rule_version": PLAN_RULE_VERSION,
        "learning_mode": scope.get("learning_mode") or "auto",
        "auto_plan": auto_plan,
        "modules_required": auto_plan and bool(planned_modules),
        "total_question_count": total,
        "items": constraints,
        "modules": module_constraints,
        "excluded": sorted(excluded_ids),
        "excluded_names": [
            db.get(KnowledgePoint, point_id).display_name
            for point_id in sorted(excluded_ids)
            if db.get(KnowledgePoint, point_id) is not None
        ],
        "unlearned": list(scope.get("unlearned") or []),
        "handout_advice": version.handout_advice or "",
        "source_scope": sources_payload,
        "unconfigured": not bool(items),
        "assessment": assessment_payload,
    }