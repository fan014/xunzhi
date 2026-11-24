from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import Attempt, LearningEvidence, Question, SourceMaterial

MASTERY_RULE_VERSION = "mastery_v2.1"
RECENT_ATTEMPT_WINDOW = 2
MIN_DISTINCT_CORRECT = 2
DEFAULT_REVIEW_AFTER_DAYS = 14

MASTERY_STATES = ("pending", "needs_reinforce", "consolidating", "stable")
STATE_LABELS = {
    "pending": "待评估",
    "needs_reinforce": "需巩固",
    "consolidating": "正在巩固",
    "stable": "相对稳定",
}
VALID_SOURCE_TYPES = {"practice", "retry"}


@dataclass
class AttemptView:
    attempt_id: str
    submitted_at: str | None
    rows: list[dict] = field(default_factory=list)

    @property
    def correct(self) -> int:
        return sum(1 for row in self.rows if row["result"] == "correct")

    @property
    def wrong(self) -> int:
        return sum(1 for row in self.rows if row["result"] == "wrong")

    @property
    def unanswered(self) -> int:
        return sum(1 for row in self.rows if row["result"] == "unanswered")

    @property
    def effective(self) -> bool:
        return self.correct + self.wrong > 0

    @property
    def time_known(self) -> bool:
        return self.submitted_at is not None


@dataclass
class MasteryResult:
    state: str
    rule_version: str = MASTERY_RULE_VERSION
    last_performance: dict | None = None
    decision_reason: str = ""
    evidence_strength: str = "weak"
    attempts_considered: int = 0
    independent_questions: int = 0
    new_question_since_previous: bool = False
    review_suggested: bool = False
    review_after_days: int = DEFAULT_REVIEW_AFTER_DAYS
    flags: list[str] = field(default_factory=list)
    review_targets: list[dict] = field(default_factory=list)
    unanswered_only: bool = False
    source_deleted: bool = False
    pending_excluded_count: int = 0

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "state_label": self.state_label,
            "rule_version": self.rule_version,
            "last_performance": self.last_performance,
            "decision_reason": self.decision_reason,
            "evidence_strength": self.evidence_strength,
            "attempts_considered": self.attempts_considered,
            "independent_questions": self.independent_questions,
            "new_question_since_previous": self.new_question_since_previous,
            "review_suggested": self.review_suggested,
            "review_after_days": self.review_after_days,
            "flags": list(self.flags),
            "review_targets": list(self.review_targets),
            "unanswered_only": self.unanswered_only,
            "source_deleted": self.source_deleted,
            "pending_excluded_count": self.pending_excluded_count,
        }


def _sort_key(view: AttemptView):
    return (view.submitted_at is not None, view.submitted_at or "", view.attempt_id)


def group_attempts(rows: list[dict], *, exclude_attempt_id: str | None = None) -> tuple[list[AttemptView], list[AttemptView]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("reliability") != "confirmed":
            continue
        if row.get("source_type") not in VALID_SOURCE_TYPES:
            continue
        if exclude_attempt_id and row.get("attempt_id") == exclude_attempt_id:
            continue
        grouped.setdefault(row["attempt_id"], []).append(row)
    effective: list[AttemptView] = []
    unanswered: list[AttemptView] = []
    for attempt_id, attempt_rows in grouped.items():
        attempt_rows.sort(key=lambda row: (row.get("ordinal") or 0, row.get("question_id") or ""))
        view = AttemptView(
            attempt_id=attempt_id,
            submitted_at=attempt_rows[0].get("submitted_at"),
            rows=attempt_rows,
        )
        if view.effective:
            effective.append(view)
        else:
            unanswered.append(view)
    effective.sort(key=_sort_key, reverse=True)
    unanswered.sort(key=_sort_key, reverse=True)
    return effective, unanswered


def question_reuse(
    question_id: str,
    earlier_question_ids: set[str],
    *,
    origin_question_id: str | None = None,
    stem_hash: str | None = None,
    earlier_origin_ids: set[str] | None = None,
    earlier_hashes: set[str] | None = None,
) -> str:
    if question_id in earlier_question_ids:
        return "original_redo"
    if origin_question_id:
        if origin_question_id in earlier_question_ids or origin_question_id in (earlier_origin_ids or set()):
            return "original_redo"
        return "unknown"
    if stem_hash and stem_hash in (earlier_hashes or set()):
        return "original_redo"
    return "new_question"


def _earlier_context(views: list[AttemptView]) -> dict:
    question_ids: set[str] = set()
    origin_ids: set[str] = set()
    hashes: set[str] = set()
    for view in views:
        for row in view.rows:
            question_ids.add(row["question_id"])
            if row.get("origin_question_id"):
                origin_ids.add(row["origin_question_id"])
            if row.get("stem_hash"):
                hashes.add(row["stem_hash"])
    return {"question_ids": question_ids, "origin_ids": origin_ids, "hashes": hashes}


def classify_attempts(attempts: list[AttemptView], unanswered_groups: list[AttemptView]) -> MasteryResult:
    if not attempts:
        return MasteryResult(
            state="pending",
            decision_reason="没有可靠的有效作答，先用少量题目补测即可。"
            if unanswered_groups
            else "还没有可用的作答证据。",
            unanswered_only=bool(unanswered_groups),
            flags=["unanswered_only"] if unanswered_groups else [],
        )

    latest = attempts[0]
    previous = _earlier_context(attempts[1:])
    window = attempts[:RECENT_ATTEMPT_WINDOW]
    distinct = {row["question_id"] for view in window for row in view.rows if row["result"] == "correct"}

    last_performance = {
        "attempt_id": latest.attempt_id,
        "submitted_at": latest.submitted_at,
        "time_known": latest.time_known,
        "correct": latest.correct,
        "wrong": latest.wrong,
        "unanswered": latest.unanswered,
        "rows": [
            {
                "question_id": row["question_id"],
                "result": row["result"],
                "ordinal": row.get("ordinal"),
            }
            for row in latest.rows
        ],
    }

    if latest.wrong > 0:
        return MasteryResult(
            state="needs_reinforce",
            last_performance=last_performance,
            decision_reason=(
                f"最近一次有效交卷中，这个考点有 {latest.wrong} 道题答错；"
                "建议先看原文回顾，再用同考点新题验证。"
            ),
            evidence_strength="weak" if len(attempts) < 2 else "moderate",
            attempts_considered=len(attempts),
            flags=["recent_wrong"],
        )

    correct_rows = [row for row in latest.rows if row["result"] == "correct"]
    reuse = [
        question_reuse(
            row["question_id"],
            previous["question_ids"],
            origin_question_id=row.get("origin_question_id"),
            stem_hash=row.get("stem_hash"),
            earlier_origin_ids=previous["origin_ids"],
            earlier_hashes=previous["hashes"],
        )
        for row in correct_rows
    ]
    all_original = bool(reuse) and all(item == "original_redo" for item in reuse)
    new_since_previous = any(
        question_reuse(
            row["question_id"],
            previous["question_ids"],
            origin_question_id=row.get("origin_question_id"),
            stem_hash=row.get("stem_hash"),
            earlier_origin_ids=previous["origin_ids"],
            earlier_hashes=previous["hashes"],
        ) == "new_question"
        for row in correct_rows
    )

    if all_original:
        return MasteryResult(
            state="consolidating",
            last_performance=last_performance,
            decision_reason="这次做对的是原题，已订正，但还需要用同考点的不同题目验证。",
            evidence_strength="weak",
            attempts_considered=len(attempts),
            independent_questions=len(distinct),
            flags=["corrected_pending_new"],
        )

    if len(attempts) < 2:
        return MasteryResult(
            state="consolidating",
            last_performance=last_performance,
            decision_reason="这次已答题都正确，但只有一次有效交卷，还不足以判断稳定。",
            evidence_strength="weak",
            attempts_considered=1,
            independent_questions=len(distinct),
            flags=["single_attempt"],
        )

    second = attempts[1]
    if second.wrong > 0:
        return MasteryResult(
            state="consolidating",
            last_performance=last_performance,
            decision_reason="最近一次全对，但上一次仍有答错，先保持巩固。",
            evidence_strength="moderate",
            attempts_considered=2,
            independent_questions=len(distinct),
            flags=["recent_wrong_in_window"],
        )

    if len(distinct) < MIN_DISTINCT_CORRECT:
        return MasteryResult(
            state="consolidating",
            last_performance=last_performance,
            decision_reason="最近两次都答对，但可区分的题目还不够两道。",
            evidence_strength="weak",
            attempts_considered=2,
            independent_questions=len(distinct),
            flags=["need_independent_questions"],
        )

    if not new_since_previous:
        return MasteryResult(
            state="consolidating",
            last_performance=last_performance,
            decision_reason="最近两次都答对，但还没有相对前次的新题验证。",
            evidence_strength="moderate",
            attempts_considered=2,
            independent_questions=len(distinct),
            flags=["corrected_pending_new"],
        )

    if not latest.time_known:
        return MasteryResult(
            state="consolidating",
            last_performance=last_performance,
            decision_reason="最近表现符合稳定条件，但提交时间未知，暂不升级为相对稳定。",
            evidence_strength="moderate",
            attempts_considered=2,
            independent_questions=len(distinct),
            flags=["time_unknown"],
        )

    return MasteryResult(
        state="stable",
        last_performance=last_performance,
        decision_reason="最近两次有效交卷都答对，且至少用一道新题验证过；可以减少重复练习。",
        evidence_strength="sufficient",
        attempts_considered=2,
        independent_questions=len(distinct),
        new_question_since_previous=True,
        flags=[],
    )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _review_suggested(last_submitted_at: str | None, now: datetime, days: int) -> bool:
    parsed = _parse_time(last_submitted_at)
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).days >= days


def _review_targets(rows: list[dict]) -> list[dict]:
    targets = []
    for row in rows:
        if row["result"] != "wrong":
            continue
        targets.append(
            {
                "skill": f"复习「{row.get('point_name') or '该考点'}」的判断依据",
                "basis_type": "wrong_answer",
                "evidence_id": row.get("evidence_id"),
                "question_id": row.get("question_id"),
                "citation_id": row.get("citation_id"),
                "surface": row.get("surface") or "question",
            }
        )
    return targets


def evaluate_point_mastery(
    db: Session,
    point,
    *,
    source_scope: list[str] | None = None,
    exclude_attempt_id: str | None = None,
    now: datetime | None = None,
    review_after_days: int = DEFAULT_REVIEW_AFTER_DAYS,
) -> MasteryResult:
    query = select(LearningEvidence).where(
        LearningEvidence.knowledge_point_id == point.id,
        LearningEvidence.reliability == "confirmed",
        LearningEvidence.source_type.in_(tuple(VALID_SOURCE_TYPES)),
    )
    if source_scope:
        query = query.where(LearningEvidence.source_id.in_(source_scope))
    evidence_rows = db.execute(query).scalars().all()

    rows: list[dict] = []
    source_ids = set()
    for evidence in evidence_rows:
        attempt = db.get(Attempt, evidence.attempt_id)
        if attempt is None or attempt.status != "submitted":
            continue
        question = db.get(Question, evidence.question_id)
        if question is None:
            continue
        source_ids.add(evidence.source_id)
        rows.append(
            {
                "attempt_id": evidence.attempt_id,
                "submitted_at": evidence.submitted_at,
                "question_id": evidence.question_id,
                "result": evidence.result,
                "reliability": evidence.reliability,
                "source_type": evidence.source_type,
                "source_id": evidence.source_id,
                "citation_id": evidence.citation_id,
                "evidence_id": evidence.id,
                "ordinal": question.ordinal,
                "origin_question_id": question.origin_question_id,
                "stem_hash": question.stem_hash,
                "point_name": point.display_name,
            }
        )

    source_scope = source_scope or [point.source_id]
    pending_excluded = db.execute(
        select(LearningEvidence).where(
            LearningEvidence.source_id.in_(source_scope),
            LearningEvidence.knowledge_point_id.is_(None),
        )
    ).scalars().all()
    source_deleted = False
    for source_id in source_ids:
        if source_id is None:
            continue
        source = db.get(SourceMaterial, source_id)
        if source is None or source.status != "active":
            source_deleted = True
            break

    attempts, unanswered = group_attempts(rows, exclude_attempt_id=exclude_attempt_id)
    result = classify_attempts(attempts, unanswered)
    result.pending_excluded_count = len(pending_excluded)
    result.source_deleted = source_deleted
    result.review_targets = _review_targets(attempts[0].rows if attempts else [])
    current = now or datetime.now(timezone.utc)
    if attempts:
        result.review_suggested = _review_suggested(
            attempts[0].submitted_at, current, review_after_days
        )
    result.review_after_days = review_after_days
    return result


def pack_mastery_changes(
    db: Session,
    pack_id: str,
    attempt_id: str,
    *,
    now: datetime | None = None,
) -> dict:
    from app.domain.models import LearningEvidence as EvidenceRow
    from app.domain.models import KnowledgePoint as PointRow

    current_rows = db.execute(
        select(EvidenceRow).where(EvidenceRow.attempt_id == attempt_id)
    ).scalars().all()
    point_ids = {row.knowledge_point_id for row in current_rows if row.knowledge_point_id}
    changes = []
    unanswered_points = []
    for point_id in sorted(point_ids):
        point = db.get(PointRow, point_id)
        if point is None:
            continue
        after = evaluate_point_mastery(db, point, now=now)
        before = evaluate_point_mastery(
            db, point, exclude_attempt_id=attempt_id, now=now
        )
        current_rows_for_point = [row for row in current_rows if row.knowledge_point_id == point_id]
        if all(row.result == "unanswered" for row in current_rows_for_point):
            unanswered_points.append(
                {
                    "knowledge_point_id": point.id,
                    "display_name": point.display_name,
                    "reason": "本次未作答，不计入答错；下次可用一道简短判断题补测。",
                }
            )
            continue
        changes.append(
            {
                "knowledge_point_id": point.id,
                "display_name": point.display_name,
                "before": before.to_dict(),
                "after": after.to_dict(),
                "changed": before.state != after.state,
            }
        )
    return {
        "pack_id": pack_id,
        "attempt_id": attempt_id,
        "rule_version": MASTERY_RULE_VERSION,
        "changes": changes,
        "unanswered_points": unanswered_points,
    }