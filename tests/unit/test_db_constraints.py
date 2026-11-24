from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.domain.models import LearningPack, ProgressSnapshot, now_iso
from app.infra.db import SessionLocal


def make_pack(db, pack_id: str, job_id: str) -> LearningPack:
    pack = LearningPack(
        id=pack_id,
        display_title="测试包",
        source_kind="real",
        difficulty="consolidate",
        question_count=3,
        job_id=job_id,
        handout_word_count=1,
        model_name="deepseek-flash",
        prompt_version="prompt_v1",
        created_at=now_iso(),
    )
    db.add(pack)
    db.add(
        ProgressSnapshot(
            pack_id=pack_id,
            raw_text="测试进度",
            difficulty="consolidate",
            question_count=3,
            created_at=now_iso(),
        )
    )
    db.commit()
    return pack


def test_progress_snapshot_cannot_be_updated():
    with SessionLocal() as db:
        make_pack(db, "pack_test_immutable", "job_test_immutable_1")
        with pytest.raises(Exception) as info:
            db.execute(text("UPDATE progress_snapshot SET raw_text='changed' WHERE pack_id='pack_test_immutable'"))
            db.commit()
        assert "immutable" in str(info.value)
        db.rollback()


def test_deleting_pack_cascades_snapshot():
    with SessionLocal() as db:
        make_pack(db, "pack_test_cascade", "job_test_cascade_1")
        db.execute(text("DELETE FROM learning_pack WHERE id='pack_test_cascade'"))
        db.commit()
        remaining = db.execute(
            text("SELECT COUNT(*) FROM progress_snapshot WHERE pack_id='pack_test_cascade'")
        ).scalar()
        assert remaining == 0


def test_generation_job_idempotency_key_is_unique():
    with SessionLocal() as db:
        db.execute(
            text(
                "INSERT INTO generation_job(id, idempotency_key, status, stage, stage_updated_at, "
                "request_json, input_hash, created_at, updated_at) VALUES "
                "('job_dup_1','dup-key','queued','fetch_sources','now','{}','h','now','now')"
            )
        )
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "INSERT INTO generation_job(id, idempotency_key, status, stage, stage_updated_at, "
                    "request_json, input_hash, created_at, updated_at) VALUES "
                    "('job_dup_2','dup-key','queued','fetch_sources','now','{}','h','now','now')"
                )
            )
            db.commit()
        db.rollback()