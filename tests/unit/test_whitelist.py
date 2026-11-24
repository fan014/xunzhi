from __future__ import annotations

import pytest

from app.domain.models import Attempt, LearningPack, Question, now_iso
from app.domain.generation import ensure_no_cross_round_duplicates, ensure_sources_active
from app.domain.schemas import ModelOutput
from app.domain.validation import ValidationError
from app.domain.schemas import stem_hash
from app.infra.db import SessionLocal


def make_pack(db, pack_id: str, job_id: str) -> LearningPack:
    pack = LearningPack(
        id=pack_id,
        display_title="父包",
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
    question = Question(
        id=f"q_{pack_id}",
        pack_id=pack_id,
        ordinal=1,
        stem="同一网段内是否需要经过路由器",
        options_json='[{"key":"A","text":"需要"},{"key":"B","text":"不需要"},{"key":"C","text":"不一定"},{"key":"D","text":"无法判断"}]',
        answer_index=1,
        explanation="同网段由交换机转发",
        knowledge_point="同网段转发",
        difficulty_tag="consolidate",
        is_image_based=False,
        stem_hash=stem_hash("同一网段内是否需要经过路由器"),
        created_at=now_iso(),
    )
    db.add(question)
    db.commit()
    return pack


def output_with_stem(stem: str) -> ModelOutput:
    return ModelOutput.model_validate(
        {
            "progress_reading": {},
            "handout": {},
            "questions": [
                {
                    "ordinal": 1,
                    "stem": stem,
                    "options": ["A", "B", "C", "D"],
                    "answer_index": 0,
                    "explanation": "解析",
                    "citations": [{"ref": "F1"}],
                }
            ],
        }
    )


def test_cross_round_duplicate_is_rejected():
    with SessionLocal() as db:
        make_pack(db, "pack_parent_dup", "job_parent_dup")
        payload = {"parent_pack_id": "pack_parent_dup"}
        with pytest.raises(ValidationError) as info:
            ensure_no_cross_round_duplicates(db, payload, output_with_stem("同一网段内是否需要经过路由器"))
        assert info.value.code == "insufficient_evidence"


def test_new_stem_passes_cross_round_check():
    with SessionLocal() as db:
        make_pack(db, "pack_parent_ok", "job_parent_ok")
        ensure_no_cross_round_duplicates(
            db, {"parent_pack_id": "pack_parent_ok"}, output_with_stem("跨网段时数据先交给谁")
        )


def test_deleted_source_blocks_persist():
    with SessionLocal() as db:
        with pytest.raises(ValidationError) as info:
            ensure_sources_active(db, {"sources": [{"source_id": "src_missing"}]})
        assert info.value.code == "source_deleted"