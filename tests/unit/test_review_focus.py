from __future__ import annotations

from app.domain.generation import create_review_focus
from app.domain.models import (
    Attempt,
    LearningPack,
    Question,
    ReviewFocus,
    now_iso,
)
from app.infra.db import SessionLocal

PACK_ID = "pack_focus_unit"
ATTEMPT_ID = "att_focus_unit"


def make_pack(db, pack_id: str, job_id: str) -> LearningPack:
    pack = LearningPack(
        id=pack_id,
        display_title="复习重点单元测试",
        source_kind="real",
        difficulty="consolidate",
        question_count=3,
        job_id=job_id,
        handout_word_count=0,
        model_name="deepseek-flash",
        prompt_version="prompt_v1",
        created_at=now_iso(),
    )
    db.add(pack)
    db.flush()
    return pack


def make_attempt(db, attempt_id: str, pack_id: str) -> Attempt:
    attempt = Attempt(
        id=attempt_id,
        pack_id=pack_id,
        status="submitted",
        idempotency_key=f"idem-{attempt_id}",
        correct_count=0,
        wrong_count=1,
        unanswered_count=0,
        total_count=1,
        submitted_at=now_iso(),
        created_at=now_iso(),
    )
    db.add(attempt)
    db.flush()
    return attempt


def make_question(db, question_id: str, pack_id: str, knowledge_point: str = "同网段转发") -> Question:
    question = Question(
        id=question_id,
        pack_id=pack_id,
        ordinal=1,
        stem="题目",
        options_json="[]",
        answer_index=0,
        explanation="解析",
        knowledge_point=knowledge_point,
        difficulty_tag="consolidate",
        is_image_based=False,
        stem_hash=f"hash-{question_id}",
        created_at="2026-11-20T00:00:00Z",
    )
    db.add(question)
    db.flush()
    return question


def test_review_focus_rows_carry_question_and_attempt():
    with SessionLocal() as db:
        make_pack(db, PACK_ID, "job_focus_unit_01")
        make_attempt(db, ATTEMPT_ID, PACK_ID)
        question = make_question(db, "q_focus_unit", PACK_ID)
        create_review_focus(db, PACK_ID, ATTEMPT_ID, [question])
        db.commit()
        row = db.query(ReviewFocus).filter(ReviewFocus.question_id == "q_focus_unit").first()
        assert row is not None
        assert row.knowledge_point == "同网段转发"
        assert row.source_pack_id == PACK_ID
        assert row.source_attempt_id == ATTEMPT_ID
        assert row.is_deleted is False


def test_review_focus_is_isolated_by_attempt():
    with SessionLocal() as db:
        make_pack(db, "pack_iso_a", "job_iso_a")
        make_attempt(db, "att_iso_a", "pack_iso_a")
        question_a = make_question(db, "q_iso_a", "pack_iso_a", "同网段转发")
        create_review_focus(db, "pack_iso_a", "att_iso_a", [question_a])

        make_pack(db, "pack_iso_b", "job_iso_b")
        make_attempt(db, "att_iso_b", "pack_iso_b")
        question_b = make_question(db, "q_iso_b", "pack_iso_b", "跨网段转发")
        create_review_focus(db, "pack_iso_b", "att_iso_b", [question_b])
        db.commit()

        rows_a = (
            db.query(ReviewFocus).filter(ReviewFocus.source_attempt_id == "att_iso_a").all()
        )
        rows_b = (
            db.query(ReviewFocus).filter(ReviewFocus.source_attempt_id == "att_iso_b").all()
        )
        assert {row.knowledge_point for row in rows_a} == {"同网段转发"}
        assert {row.knowledge_point for row in rows_b} == {"跨网段转发"}
        assert all(row.source_pack_id == "pack_iso_a" for row in rows_a)
        assert all(row.source_pack_id == "pack_iso_b" for row in rows_b)
        assert all(row.question_id == "q_iso_a" for row in rows_a)
        assert all(row.question_id == "q_iso_b" for row in rows_b)