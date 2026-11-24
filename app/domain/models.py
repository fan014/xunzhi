from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class SourceMaterial(Base):
    __tablename__ = "source_material"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    original_name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    ext: Mapped[str] = mapped_column(String, nullable=False)
    mime: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    stored_path: Mapped[str] = mapped_column(String, nullable=False)
    cleanup_pending: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text_index_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    text_index_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)


Index(
    "uq_source_active_sha",
    SourceMaterial.sha256,
    SourceMaterial.size_bytes,
    unique=True,
    sqlite_where=SourceMaterial.status == "active",
)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_base"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


class KnowledgeBaseItem(Base):
    __tablename__ = "knowledge_base_item"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(
        String, ForeignKey("knowledge_base.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("source_material.id", ondelete="CASCADE"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


Index("ix_kb_item_base", KnowledgeBaseItem.knowledge_base_id, KnowledgeBaseItem.position)
Index("ix_kb_item_source", KnowledgeBaseItem.source_id)


class SourcePage(Base):
    __tablename__ = "source_page"

    source_id: Mapped[str] = mapped_column(
        String, ForeignKey("source_material.id", ondelete="CASCADE"), primary_key=True
    )
    page_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    has_text: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_scanned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    text_block_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_image_asset_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("image_asset.id", ondelete="SET NULL"), nullable=True
    )


class ImageAsset(Base):
    __tablename__ = "image_asset"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String, ForeignKey("source_material.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rel_path: Mapped[str] = mapped_column(String, nullable=False)
    mime: Mapped[str] = mapped_column(String, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String, ForeignKey("source_material.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    locator_json: Mapped[str] = mapped_column(Text, nullable=False)
    quote_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[str] = mapped_column(String, nullable=False, default="high")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")


class LearningPack(Base):
    __tablename__ = "learning_pack"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_title: Mapped[str] = mapped_column(String, nullable=False)
    source_kind: Mapped[str] = mapped_column(String, nullable=False, default="real")
    difficulty: Mapped[str] = mapped_column(String, nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_pack_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_label: Mapped[str | None] = mapped_column(String, nullable=True)
    job_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    handout_word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_name: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


class PackSource(Base):
    __tablename__ = "pack_source"

    pack_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_pack.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(
        String, ForeignKey("source_material.id", ondelete="RESTRICT"), primary_key=True
    )
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_count_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_name_snapshot: Mapped[str] = mapped_column(String, nullable=False)
    source_sha256_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind_snapshot: Mapped[str] = mapped_column(String, nullable=False)


class ProgressSnapshot(Base):
    __tablename__ = "progress_snapshot"

    pack_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_pack.id", ondelete="CASCADE"), primary_key=True
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    difficulty: Mapped[str] = mapped_column(String, nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    learned_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    unlearned_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    weak_points_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    intent_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    insufficient: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conflict_notes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    review_focus_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


class HandoutSection(Base):
    __tablename__ = "handout_section"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    pack_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_pack.id", ondelete="CASCADE"), nullable=False
    )
    section_type: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(String, nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    section_role: Mapped[str] = mapped_column(String, nullable=False, default="neutral")
    is_extension: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    module_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Question(Base):
    __tablename__ = "question"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    pack_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_pack.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False)
    answer_index: Mapped[int] = mapped_column(Integer, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_point: Mapped[str] = mapped_column(String, nullable=False)
    module_id: Mapped[str | None] = mapped_column(String, nullable=True)
    origin_question_id: Mapped[str | None] = mapped_column(String, nullable=True)
    variant_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    difficulty_tag: Mapped[str] = mapped_column(String, nullable=False)
    is_image_based: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stem_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)

    __table_args__ = (
        UniqueConstraint("pack_id", "ordinal", name="uq_question_pack_ordinal"),
    )


Index("ix_question_origin", Question.origin_question_id)


class Citation(Base):
    __tablename__ = "citation"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    pack_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_pack.id", ondelete="CASCADE"), nullable=False
    )
    owner_type: Mapped[str] = mapped_column(String, nullable=False)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    evidence_id: Mapped[str] = mapped_column(
        String, ForeignKey("evidence.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locator_json: Mapped[str] = mapped_column(Text, nullable=False)
    quote_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    local_ref: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


Index("ix_citation_owner", Citation.owner_type, Citation.owner_id)
Index("ix_citation_pack", Citation.pack_id)


class Attempt(Base):
    __tablename__ = "attempt"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    pack_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_pack.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    schedule_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    correct_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wrong_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unanswered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    submitted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)

    __table_args__ = (
        UniqueConstraint("pack_id", "idempotency_key", name="uq_attempt_idem"),
    )


Index(
    "uq_attempt_draft_whole",
    Attempt.pack_id,
    unique=True,
    sqlite_where=(Attempt.status == "draft") & Attempt.schedule_task_id.is_(None),
)
Index(
    "uq_attempt_draft_task",
    Attempt.pack_id,
    Attempt.schedule_task_id,
    unique=True,
    sqlite_where=(Attempt.status == "draft") & Attempt.schedule_task_id.is_not(None),
)


class AttemptAnswer(Base):
    __tablename__ = "attempt_answer"

    attempt_id: Mapped[str] = mapped_column(
        String, ForeignKey("attempt.id", ondelete="CASCADE"), primary_key=True
    )
    question_id: Mapped[str] = mapped_column(
        String, ForeignKey("question.id", ondelete="CASCADE"), primary_key=True
    )
    selected_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    answered_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


class ReviewFocus(Base):
    __tablename__ = "review_focus"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_pack_id: Mapped[str] = mapped_column(String, nullable=False)
    source_attempt_id: Mapped[str] = mapped_column(String, nullable=False)
    question_id: Mapped[str] = mapped_column(String, nullable=False)
    knowledge_point_id: Mapped[str | None] = mapped_column(String, nullable=True)
    knowledge_point: Mapped[str] = mapped_column(String, nullable=False)
    suggestion: Mapped[str | None] = mapped_column(String, nullable=True)
    edited_text: Mapped[str | None] = mapped_column(String, nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    used_in_pack_id: Mapped[str | None] = mapped_column(String, nullable=True)


class GenerationJob(Base):
    __tablename__ = "generation_job"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String, nullable=False, default="fetch_sources")
    stage_updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pack_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    # 派发状态：NULL（旧任务）/pending_dispatch（已落库未派发）/
    # dispatching（已取派发权、执行器未接手）/dispatched（执行器已接手，与 running 同语句写入）。
    # 该列不表达执行结果，执行结果仍由 status 六态表达。
    launch_state: Mapped[str | None] = mapped_column(String, nullable=True)


class GenerationBatchCache(Base):
    __tablename__ = "generation_batch_cache"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    plan_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    job_id: Mapped[str] = mapped_column(String, nullable=False)
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    batch_json: Mapped[str] = mapped_column(Text, nullable=False)
    ref_map_json: Mapped[str] = mapped_column(Text, nullable=False)
    usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    completed_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


class GenerationEvidence(Base):
    __tablename__ = "generation_evidence"

    job_id: Mapped[str] = mapped_column(
        String, ForeignKey("generation_job.id", ondelete="CASCADE"), primary_key=True
    )
    local_ref: Mapped[str] = mapped_column(String, primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    sent_as: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class OfflineState(Base):
    __tablename__ = "offline_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    sample_version: Mapped[str] = mapped_column(String, nullable=False, default="1")
    round_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    submitted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    answers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    focus_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


class AppSetting(Base):
    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)

class KnowledgePoint(Base):
    __tablename__ = "knowledge_point"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String, ForeignKey("source_material.id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    name_normalized: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)

    __table_args__ = (
        UniqueConstraint("source_id", "name_normalized", name="uq_kp_source_name"),
    )


class QuestionKnowledgePoint(Base):
    __tablename__ = "question_knowledge_point"

    question_id: Mapped[str] = mapped_column(
        String, ForeignKey("question.id", ondelete="CASCADE"), primary_key=True
    )
    knowledge_point_id: Mapped[str] = mapped_column(
        String, ForeignKey("knowledge_point.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String, primary_key=True, default="primary")
    reliability: Mapped[str] = mapped_column(String, nullable=False, default="confirmed")
    citation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("citation.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)

    __table_args__ = (
        UniqueConstraint("question_id", "knowledge_point_id", "role", name="uq_qkp"),
    )


Index(
    "uq_qkp_primary",
    QuestionKnowledgePoint.question_id,
    unique=True,
    sqlite_where=QuestionKnowledgePoint.role == "primary",
)


class LearningEvidence(Base):
    __tablename__ = "learning_evidence"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    pack_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_pack.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(
        String, ForeignKey("attempt.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[str] = mapped_column(
        String, ForeignKey("question.id", ondelete="CASCADE"), nullable=False
    )
    knowledge_point_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("knowledge_point.id", ondelete="RESTRICT"), nullable=True
    )
    source_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("source_material.id", ondelete="RESTRICT"), nullable=True
    )
    citation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("citation.id", ondelete="SET NULL"), nullable=True
    )
    diagnostic_answer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    result: Mapped[str] = mapped_column(String, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False, default="practice")
    reliability: Mapped[str] = mapped_column(String, nullable=False, default="confirmed")
    submitted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)

    __table_args__ = (
        UniqueConstraint("attempt_id", "question_id", name="uq_evidence_attempt_question"),
    )


Index("ix_evidence_point", LearningEvidence.knowledge_point_id)
Index("ix_evidence_pack", LearningEvidence.pack_id)
Index("ix_evidence_attempt", LearningEvidence.attempt_id)
Index("ix_evidence_point_time", LearningEvidence.knowledge_point_id, LearningEvidence.submitted_at)
Index("ix_evidence_source_time", LearningEvidence.source_id, LearningEvidence.submitted_at)


class LearningPlan(Base):
    __tablename__ = "learning_plan"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    current_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    primary_source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


class LearningPlanVersion(Base):
    __tablename__ = "learning_plan_version"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_plan.id", ondelete="CASCADE"), nullable=False
    )
    previous_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    origin: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    base_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    suggestion_key: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    adjustment_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    sources_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    scope_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    modules_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostic_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    assessment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    question_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handout_advice: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    confirmed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class ScheduleTask(Base):
    __tablename__ = "schedule_task"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_plan.id", ondelete="CASCADE"), nullable=False
    )
    plan_version_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_plan_version.id", ondelete="CASCADE"), nullable=False
    )
    module_id: Mapped[str] = mapped_column(String, nullable=False)
    module_title: Mapped[str] = mapped_column(String, nullable=False, default="")
    task_title: Mapped[str] = mapped_column(String, nullable=False, default="")
    task_type: Mapped[str] = mapped_column(String, nullable=False, default="reading")
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_date: Mapped[str] = mapped_column(String, nullable=False)
    estimated_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    planned_question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    question_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    question_pack_id: Mapped[str | None] = mapped_column(String, nullable=True)
    knowledge_point_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completion_source: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("attempt.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)

    __table_args__ = (
        UniqueConstraint("plan_version_id", "ordinal", name="uq_schedule_version_ordinal"),
    )


Index("ix_schedule_version", ScheduleTask.plan_version_id, ScheduleTask.ordinal)
Index("ix_schedule_date", ScheduleTask.scheduled_date)
Index("ix_schedule_status", ScheduleTask.status)


Index("ix_plan_version_plan", LearningPlanVersion.plan_id)
Index("ix_plan_version_base", LearningPlanVersion.base_version_id)
Index(
    "uq_plan_version_suggestion",
    LearningPlanVersion.suggestion_key,
    unique=True,
)
Index("ix_plan_source_status", LearningPlan.primary_source_id, LearningPlan.status)


class LearnerProfileSnapshot(Base):
    __tablename__ = "learner_profile_snapshot"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    learner_key: Mapped[str] = mapped_column(String, nullable=False, default="local")
    source_id: Mapped[str] = mapped_column(
        String, ForeignKey("source_material.id", ondelete="RESTRICT"), nullable=False
    )
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("attempt.id", ondelete="SET NULL"), nullable=True
    )
    evidence_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    rule_version: Mapped[str] = mapped_column(String, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)


Index("ix_profile_source_status", LearnerProfileSnapshot.source_id, LearnerProfileSnapshot.status)
Index("ix_profile_fingerprint", LearnerProfileSnapshot.evidence_fingerprint)
Index("ix_profile_attempt", LearnerProfileSnapshot.attempt_id)


class PackPlanVersion(Base):
    __tablename__ = "pack_plan_version"


    pack_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_pack.id", ondelete="CASCADE"), primary_key=True
    )
    plan_version_id: Mapped[str] = mapped_column(
        String, ForeignKey("learning_plan_version.id", ondelete="RESTRICT"), nullable=False
    )
    adopted_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    delivery_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class KnowledgePointMapping(Base):
    __tablename__ = "knowledge_point_mapping"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    from_point_id: Mapped[str] = mapped_column(
        String, ForeignKey("knowledge_point.id", ondelete="CASCADE"), nullable=False
    )
    to_point_id: Mapped[str] = mapped_column(
        String, ForeignKey("knowledge_point.id", ondelete="CASCADE"), nullable=False
    )
    basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)


Index("ix_kp_mapping_from", KnowledgePointMapping.from_point_id)


class SelfAssessment(Base):
    __tablename__ = "self_assessment"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    knowledge_point_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("knowledge_point.id", ondelete="SET NULL"), nullable=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    diagnostic_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="note")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


class DiagnosticSession(Base):
    __tablename__ = "diagnostic_session"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(
        String, ForeignKey("source_material.id", ondelete="RESTRICT"), nullable=False
    )
    target_goal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    current_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_key: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    pending_items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    plan_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    knowledge_base_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_scope_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    confirmed_at: Mapped[str | None] = mapped_column(String, nullable=True)


Index("ix_diagnosis_source_status", DiagnosticSession.source_id, DiagnosticSession.status)


class DiagnosticRound(Base):
    __tablename__ = "diagnostic_round"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("diagnostic_session.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    intent: Mapped[str] = mapped_column(String, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)

    __table_args__ = (
        UniqueConstraint("session_id", "ordinal", name="uq_diagnostic_round_ordinal"),
    )


class DiagnosticQuestion(Base):
    __tablename__ = "diagnostic_question"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("diagnostic_session.id", ondelete="CASCADE"), nullable=False
    )
    round_id: Mapped[str] = mapped_column(
        String, ForeignKey("diagnostic_round.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="scope")
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    answer_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    knowledge_point_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("knowledge_point.id", ondelete="SET NULL"), nullable=True
    )
    knowledge_point_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    citation_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)


Index("ix_diagnostic_question_session", DiagnosticQuestion.session_id, DiagnosticQuestion.status)


class DiagnosticAnswer(Base):
    __tablename__ = "diagnostic_answer"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String, ForeignKey("diagnostic_session.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[str] = mapped_column(
        String, ForeignKey("diagnostic_question.id", ondelete="CASCADE"), nullable=False
    )
    selected_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="answered")
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    answer_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)

    __table_args__ = (
        UniqueConstraint("session_id", "question_id", name="uq_diagnostic_answer_question"),
    )


Index("uq_diagnostic_answer_key", DiagnosticAnswer.answer_key, unique=True)


class SchemaMigration(Base):
    __tablename__ = "schema_migration"

    version: Mapped[str] = mapped_column(String, primary_key=True)
    applied_at: Mapped[str] = mapped_column(String, nullable=False, default=now_iso)
    linked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")