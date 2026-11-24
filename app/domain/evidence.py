from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import mastery
from app.domain.models import (
    Attempt,
    Citation,
    KnowledgePoint,
    LearningEvidence,
    LearningPack,
    PackSource,
    Question,
    QuestionKnowledgePoint,
    SourceMaterial,
    now_iso,
)
from app.infra.ids import new_id


def _point_for_question(db: Session, question_id: str) -> QuestionKnowledgePoint | None:
    return db.execute(
        select(QuestionKnowledgePoint).where(
            QuestionKnowledgePoint.question_id == question_id,
            QuestionKnowledgePoint.role == "primary",
        )
    ).scalars().first()


def _citation_for_question(db: Session, question_id: str) -> Citation | None:
    return db.execute(
        select(Citation)
        .where(Citation.owner_type == "question", Citation.owner_id == question_id)
        .order_by(Citation.id)
    ).scalars().first()


def _fallback_source(db: Session, pack_id: str) -> str | None:
    link = db.execute(
        select(PackSource).where(PackSource.pack_id == pack_id).order_by(PackSource.source_id)
    ).scalars().first()
    return link.source_id if link is not None else None


def record_attempt_evidence(db: Session, pack: LearningPack, attempt: Attempt, graded) -> int:
    questions = db.execute(
        select(Question).where(Question.pack_id == pack.id).order_by(Question.ordinal)
    ).scalars().all()
    by_question_id = {question.id: question for question in questions}
    existing = {
        row.question_id
        for row in db.execute(
            select(LearningEvidence).where(LearningEvidence.attempt_id == attempt.id)
        ).scalars().all()
    }
    source_type = "retry" if pack.parent_pack_id else "practice"
    fallback = _fallback_source(db, pack.id)
    written = 0
    for graded_item in graded.answers:
        question = by_question_id.get(graded_item.question_id)
        if question is None or question.id in existing:
            continue
        if graded_item.selected_index is None:
            result = "unanswered"
        elif graded_item.is_correct:
            result = "correct"
        else:
            result = "wrong"
        point_link = _point_for_question(db, question.id)
        citation = _citation_for_question(db, question.id)
        db.add(
            LearningEvidence(
                id=new_id("learning_evidence"),
                pack_id=pack.id,
                attempt_id=attempt.id,
                question_id=question.id,
                knowledge_point_id=point_link.knowledge_point_id if point_link is not None else None,
                source_id=citation.source_id if citation is not None else fallback,
                citation_id=citation.id if citation is not None else None,
                result=result,
                source_type=source_type,
                reliability=point_link.reliability if point_link is not None else "pending",
                submitted_at=attempt.submitted_at,
                created_at=now_iso(),
            )
        )
        written += 1
    db.flush()
    return written


def _ordered_evidence(db: Session, point_id: str) -> list[LearningEvidence]:
    return db.execute(
        select(LearningEvidence)
        .where(
            LearningEvidence.knowledge_point_id == point_id,
            LearningEvidence.source_type.in_(("practice", "retry")),
        )
        .order_by(
            LearningEvidence.submitted_at.desc(),
            LearningEvidence.created_at.desc(),
            LearningEvidence.id.desc(),
        )
    ).scalars().all()


def _diagnostic_evidence(db: Session, point_id: str) -> list[LearningEvidence]:
    return db.execute(
        select(LearningEvidence)
        .where(
            LearningEvidence.knowledge_point_id == point_id,
            LearningEvidence.source_type == "diagnostic",
        )
        .order_by(
            LearningEvidence.submitted_at.desc(),
            LearningEvidence.created_at.desc(),
            LearningEvidence.id.desc(),
        )
    ).scalars().all()


def _mastery(rows: list[LearningEvidence]) -> str:
    if not rows:
        return "pending"
    latest_attempt = rows[0].attempt_id
    latest_rows = [row for row in rows if row.attempt_id == latest_attempt]
    if any(row.result != "correct" for row in latest_rows):
        return "needs_reinforce"
    non_correct = sum(1 for row in rows if row.result != "correct")
    distinct_questions = len({row.question_id for row in rows})
    if non_correct == 0 and distinct_questions >= 2:
        return "stable"
    return "consolidating"


def evidence_payload(db: Session, row: LearningEvidence) -> dict:
    attempt = db.get(Attempt, row.attempt_id)
    question = db.get(Question, row.question_id)
    citation = db.get(Citation, row.citation_id) if row.citation_id else None
    source = db.get(SourceMaterial, row.source_id) if row.source_id else None
    return {
        "id": row.id,
        "pack_id": row.pack_id,
        "attempt_id": row.attempt_id,
        "question_id": row.question_id,
        "knowledge_point_id": row.knowledge_point_id,
        "source_id": row.source_id,
        "citation_id": row.citation_id,
        "result": row.result,
        "source_type": row.source_type,
        "reliability": row.reliability,
        "submitted_at": row.submitted_at,
        "time_known": row.submitted_at is not None,
        "question": (
            {
                "id": question.id,
                "ordinal": question.ordinal,
                "stem": question.stem[:80],
                "knowledge_point": question.knowledge_point,
                "origin_question_id": question.origin_question_id,
            }
            if question is not None
            else None
        ),
        "citation": (
            {
                "id": citation.id,
                "source_name": source.display_name if source is not None else "来源资料已删除",
                "page_no": citation.page_no,
                "claim": citation.claim_text,
                "status": source.status if source is not None else "deleted",
            }
            if citation is not None
            else None
        ),
        "question_reuse": "unknown",
        "attempt_score": (
            {
                "correct": attempt.correct_count,
                "wrong": attempt.wrong_count,
                "unanswered": attempt.unanswered_count,
                "total": attempt.total_count,
            }
            if attempt is not None
            else None
        ),
    }


def summary_for_point(db: Session, point: KnowledgePoint) -> dict:
    rows = _ordered_evidence(db, point.id)
    diagnostics = _diagnostic_evidence(db, point.id)
    latest = rows[0] if rows else None
    retry_rows = [row for row in rows if row.source_type == "retry"]
    retry_latest = retry_rows[0] if retry_rows else None
    attempt_ids = {row.attempt_id for row in rows}
    result = mastery.evaluate_point_mastery(db, point)
    items = []
    prior_questions: set[str] = set()
    prior_origins: set[str] = set()
    prior_hashes: set[str] = set()
    for row in reversed(rows):
        question = db.get(Question, row.question_id)
        payload = evidence_payload(db, row)
        payload["question_reuse"] = mastery.question_reuse(
            row.question_id,
            prior_questions,
            origin_question_id=question.origin_question_id if question is not None else None,
            stem_hash=question.stem_hash if question is not None else None,
            earlier_origin_ids=prior_origins,
            earlier_hashes=prior_hashes,
        )
        items.append(payload)
        prior_questions.add(row.question_id)
        if question is not None and question.origin_question_id:
            prior_origins.add(question.origin_question_id)
        if question is not None and question.stem_hash:
            prior_hashes.add(question.stem_hash)
    items.reverse()
    return {
        "knowledge_point": {
            "id": point.id,
            "source_id": point.source_id,
            "display_name": point.display_name,
            "status": point.status,
        },
        "summary": {
            "mastery_status": result.state,
            "mastery_rule": mastery.MASTERY_RULE_VERSION,
            "mastery": result.to_dict(),
            "last_result": latest.result if latest is not None else None,
            "last_submitted_at": latest.submitted_at if latest is not None else None,
            "last_time_known": bool(latest is not None and latest.submitted_at is not None),
            "correct": sum(1 for row in rows if row.result == "correct"),
            "wrong": sum(1 for row in rows if row.result == "wrong"),
            "unanswered": sum(1 for row in rows if row.result == "unanswered"),
            "evidence_count": len(rows),
            "attempt_count": len(attempt_ids),
            "retry_count": len(retry_rows),
            "last_retry": (
                {
                    "attempt_id": retry_latest.attempt_id,
                    "pack_id": retry_latest.pack_id,
                    "result": retry_latest.result,
                    "submitted_at": retry_latest.submitted_at,
                    "time_known": retry_latest.submitted_at is not None,
                }
                if retry_latest is not None
                else None
            ),
        },
        "items": items,
        "by_source": {
            "practice": items,
            "diagnostic": [evidence_payload(db, row) for row in diagnostics],
        },
    }


def pack_evidence(db: Session, pack_id: str) -> dict:
    rows = db.execute(
        select(LearningEvidence)
        .where(LearningEvidence.pack_id == pack_id)
        .order_by(LearningEvidence.submitted_at.desc(), LearningEvidence.id.desc())
    ).scalars().all()
    return {
        "pack_id": pack_id,
        "items": [evidence_payload(db, row) for row in rows],
        "pending_count": sum(1 for row in rows if row.knowledge_point_id is None),
    }