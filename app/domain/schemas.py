from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

DIFFICULTIES = {"basic", "consolidate"}
LEARNING_MODES = {"auto", "foundation", "focus"}


class SourceSpec(BaseModel):
    source_id: str
    page_start: int | None = None
    page_end: int | None = None


class CreatePackRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=120)
    sources: list[SourceSpec] = Field(default_factory=list)
    progress_text: str
    difficulty: str = "consolidate"
    question_count: int | None = None
    consent_ack: bool = False
    parent_pack_id: str | None = None
    review_focus: list[str] = Field(default_factory=list)
    plan_version_id: str | None = None

    @field_validator("difficulty")
    @classmethod
    def _difficulty(cls, value: str) -> str:
        if value not in DIFFICULTIES:
            raise ValueError("difficulty must be basic or consolidate")
        return value

    @field_validator("question_count")
    @classmethod
    def _count(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not 1 <= value <= 60:
            raise ValueError("question_count must be between 1 and 60")
        return value


class AnswerItem(BaseModel):
    question_id: str
    selected_index: int | None = None

    @field_validator("selected_index")
    @classmethod
    def _index(cls, value: int | None) -> int | None:
        if value is not None and not 0 <= value <= 3:
            raise ValueError("selected_index must be 0..3 or null")
        return value


class DraftRequest(BaseModel):
    answers: list[AnswerItem] = Field(default_factory=list)


class SubmitRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=120)
    answers: list[AnswerItem] = Field(default_factory=list)
    task_id: str | None = None


class PackPositionRequest(BaseModel):
    module_id: str | None = None
    page_id: str | None = None
    view: str = "modules"

    @field_validator("view")
    @classmethod
    def _view(cls, value: str) -> str:
        if value not in {"handout", "modules", "review", "questions"}:
            raise ValueError("view must be handout, modules, review or questions")
        return value


class ConfirmRequest(BaseModel):
    confirm: bool = False


class ReviewFocusText(BaseModel):
    text: str = ""


class ReviewFocusItem(BaseModel):
    knowledge_point: str
    suggestion: str = ""


class KnowledgePointRename(BaseModel):
    display_name: str


class PlanSourceItem(BaseModel):
    source_id: str
    page_start: int | None = None
    page_end: int | None = None


class PlanBasisItem(BaseModel):
    type: str
    ref_type: str | None = None
    ref_id: str | None = None


class PlanPointItem(BaseModel):
    knowledge_point_id: str | None = None
    display_name: str = ""
    priority: str = "review"
    suggested_question_count: int = 0
    basis: list[PlanBasisItem] = Field(default_factory=list)


class PlanCreateRequest(BaseModel):
    title: str = "学习计划"
    sources: list[PlanSourceItem] = Field(default_factory=list)
    learned: list[str] = Field(default_factory=list)
    unlearned: list[str] = Field(default_factory=list)
    goal: str = ""
    points: list[PlanPointItem] = Field(default_factory=list)
    question_count: int | None = None
    learning_mode: str = "auto"
    handout_advice: str = ""
    unlearned_point_ids: list[str] = Field(default_factory=list)

    @field_validator("learning_mode")
    @classmethod
    def _learning_mode(cls, value: str) -> str:
        if value not in LEARNING_MODES:
            raise ValueError("learning_mode must be auto, foundation or focus")
        return value


class PlanVersionRequest(PlanCreateRequest):
    pass


class SuggestPlanRequest(PlanCreateRequest):
    previous_version_id: str | None = None
    unlearned_point_ids: list[str] = Field(default_factory=list)
    preserve: dict = Field(default_factory=dict)
    force_recompute: bool = False
    page_start: int | None = None
    page_end: int | None = None


class DiagnosisStartRequest(BaseModel):
    idempotency_key: str
    goal: str = ""
    question_count: int | None = None
    learning_mode: str = "auto"
    knowledge_base_id: str | None = None
    scope_mode: str = "all"
    source_ids: list[str] = Field(default_factory=list)
    page_ranges: dict[str, dict] = Field(default_factory=dict)
    direct_view: bool = False

    @field_validator("learning_mode")
    @classmethod
    def _learning_mode(cls, value: str) -> str:
        if value not in LEARNING_MODES:
            raise ValueError("learning_mode must be auto, foundation or focus")
        return value


class DiagnosisAnswerRequest(BaseModel):
    idempotency_key: str
    question_id: str
    selected_index: int | None = None
    status: str = "answered"


class ScheduleOptions(BaseModel):
    start_date: str | None = None
    duration_days: int | None = None
    target_date: str | None = None
    daily_minutes: int | None = None


class SchedulePreviewRequest(ScheduleOptions):
    plan_version_id: str


class PlanScheduleRequest(ScheduleOptions):
    pass


class DiagnosisPlanRequest(ScheduleOptions):
    goal: str = ""
    question_count: int | None = None
    learning_mode: str = "auto"

    @field_validator("learning_mode")
    @classmethod
    def _learning_mode(cls, value: str) -> str:
        if value not in LEARNING_MODES:
            raise ValueError("learning_mode must be auto, foundation or focus")
        return value


class DiagnosisConfirmRequest(ScheduleOptions):
    idempotency_key: str


class AssessmentRequest(BaseModel):
    attempt_id: str
    force: bool = False
    source_id: str | None = None


class ScheduleShiftRequest(BaseModel):
    plan_id: str | None = None
    version_id: str | None = None
    preview_key: str | None = None


class ScheduleReschedulePreviewRequest(BaseModel):
    scheduled_date: str


class ScheduleRescheduleRequest(BaseModel):
    scheduled_date: str
    expected_date: str | None = None


class KnowledgeBaseCreateRequest(BaseModel):
    name: str
    description: str = ""


class KnowledgeBaseUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class KnowledgeBaseItemAddRequest(BaseModel):
    source_id: str
    note: str = ""


class KnowledgeBaseItemUpdateRequest(BaseModel):
    display_name: str | None = None
    note: str | None = None


class CitationRef(BaseModel):
    ref: str
    claim: str = ""
    box_2d: list[float] | None = None
    image_description: str | None = None


class HandoutBlock(BaseModel):
    heading: str = ""
    body: str
    role: str = "neutral"
    citations: list[CitationRef] = Field(default_factory=list)


class Scope(BaseModel):
    covered: list[str] = Field(default_factory=list)
    not_covered: list[str] = Field(default_factory=list)


PAGE_TYPES = {"text", "source_asset", "comparison", "step_sequence"}


class VisualCard(BaseModel):
    title: str = ""
    condition: str = ""
    example: str = ""
    result: str = ""


class VisualNode(BaseModel):
    id: str
    label: str = ""
    kind: str = "process"
    state: str = ""


class VisualEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str = ""


class VisualStep(BaseModel):
    text: str = ""
    active_node_ids: list[str] = Field(default_factory=list)
    active_edge_ids: list[str] = Field(default_factory=list)
    node_states: dict[str, str] = Field(default_factory=dict)


class VisualOption(BaseModel):
    id: str
    text: str = ""


class VisualMode(BaseModel):
    id: str
    label: str = ""
    correct_option_id: str = ""
    steps: list[VisualStep] = Field(default_factory=list)
    cards: list[VisualCard] = Field(default_factory=list)


class PageVisual(BaseModel):
    type: str = "text"
    title: str = ""
    caption: str = ""
    alt_text: str = ""
    source_ref: str | None = None
    cards: list[VisualCard] = Field(default_factory=list)
    nodes: list[VisualNode] = Field(default_factory=list)
    edges: list[VisualEdge] = Field(default_factory=list)
    steps: list[VisualStep] = Field(default_factory=list)
    modes: list[VisualMode] = Field(default_factory=list)
    options: list[VisualOption] = Field(default_factory=list)
    correct_option_id: str = ""
    preset_id: str | None = None
    notice: str = ""


class LearningPage(BaseModel):
    page_id: str = ""
    knowledge_point_ids: list[str] = Field(default_factory=list)
    title: str = ""
    explanation: str = ""
    visual: PageVisual | None = None
    related_sources: list[CitationRef] = Field(default_factory=list)


class RecapRule(BaseModel):
    condition: str = ""
    result: str = ""
    pitfall: str = ""


class ModuleRecap(BaseModel):
    rules: list[RecapRule] = Field(default_factory=list)
    cross_module: str = ""
    summary: str = ""


class Handout(BaseModel):
    scope: Scope = Field(default_factory=Scope)
    key_points: list[HandoutBlock] = Field(default_factory=list)
    pitfalls: list[HandoutBlock] = Field(default_factory=list)
    visual: list[HandoutBlock] = Field(default_factory=list)


class ProgressReading(BaseModel):
    learned: list[str] = Field(default_factory=list)
    unlearned: list[str] = Field(default_factory=list)
    weak_points: list[str] = Field(default_factory=list)
    intent: list[str] = Field(default_factory=list)
    insufficient: bool = False


class ModelQuestion(BaseModel):
    ordinal: int
    stem: str
    options: list[str]
    answer_index: int
    explanation: str
    knowledge_point: str = ""
    knowledge_point_id: str = ""
    module_id: str = ""
    difficulty_tag: str = "consolidate"
    is_image_based: bool = False
    citations: list[CitationRef] = Field(default_factory=list)

    @field_validator("options")
    @classmethod
    def _options(cls, value: list[str]) -> list[str]:
        if len(value) != 4:
            raise ValueError("question must have exactly 4 options")
        normalized = [re.sub(r"\s+", "", item) for item in value]
        if len(set(normalized)) != 4:
            raise ValueError("question options must be distinct")
        return value


class InsufficientEvidence(BaseModel):
    supportable_question_count: int
    reason: str = ""
    suggested_pages: str = ""


class ModelModule(BaseModel):
    module_id: str
    title: str = ""
    objective: str = ""
    core_points: list[str] = Field(default_factory=list)
    key_points: list[HandoutBlock] = Field(default_factory=list)
    pitfalls: list[HandoutBlock] = Field(default_factory=list)
    visual: list[HandoutBlock] = Field(default_factory=list)
    pages: list[LearningPage] = Field(default_factory=list)
    recap: ModuleRecap | None = None
    completion: str = ""


class ModelOutput(BaseModel):
    progress_reading: ProgressReading = Field(default_factory=ProgressReading)
    handout: Handout = Field(default_factory=Handout)
    modules: list[ModelModule] = Field(default_factory=list)
    questions: list[ModelQuestion] = Field(default_factory=list)
    insufficient_evidence: InsufficientEvidence | None = None


@dataclass
class RefTarget:
    evidence_id: str
    source_id: str
    kind: str
    page_no: int | None
    confidence: str
    locator_json: str
    quote_text: str | None
    label_text: str | None = None


@dataclass
class RefMap:
    targets: dict[str, RefTarget] = field(default_factory=dict)

    def get(self, ref: str) -> RefTarget | None:
        return self.targets.get(ref)


def stem_hash(stem: str) -> str:
    normalized = re.sub(r"[\s\W_]+", "", stem).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def word_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+", text))