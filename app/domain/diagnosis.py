from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.domain import plans as plan_service
from app.domain import schedule as schedule_service
from app.domain.generation import assemble_for_sources
from app.domain.knowledge_base import chapters_for_source
from app.domain.knowledge import (
    is_fragment_point,
    ensure_knowledge_point,
    find_knowledge_point,
    is_generic_point,
    normalize_point_name,
)
from app.infra.model_client import (
    ModelError,
    ModelRequest,
    TextPayload,
    get_model_client,
    is_fake_client,
)
from app.domain.models import (
    Attempt,
    AttemptAnswer,
    Citation,
    DiagnosticAnswer,
    DiagnosticQuestion,
    DiagnosticRound,
    DiagnosticSession,
    Evidence,
    GenerationJob,
    ImageAsset,
    KnowledgePoint,
    LearningEvidence,
    LearningPack,
    LearningPlan,
    LearningPlanVersion,
    PackSource,
    Question,
    SelfAssessment,
    SourceMaterial,
    SourcePage,
    now_iso,
)
from app.domain.schemas import PlanCreateRequest, PlanPointItem, PlanSourceItem, stem_hash
from app.domain.validation import ValidationError
from app.infra.ids import new_id

DIAGNOSIS_RULE_VERSION = "diagnosis_v1"
DEFAULT_ROUNDS = 3
MAX_ROUNDS = 4
DEFAULT_ROUNDS_TO_COUNT = 3
MAX_GENERATION_ATTEMPTS = 3
JOB_FAILED_STATUSES = {"failed", "cancelled", "insufficient_evidence"}
JOB_TERMINAL_STATUSES = {"succeeded", *JOB_FAILED_STATUSES}

logger = logging.getLogger("xunzhi.diagnosis")


class DiagnosisError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DiagnosisParseError(DiagnosisError):
    def __init__(
        self,
        message: str,
        *,
        position: int | None = None,
        object_count: int = 0,
        reason: str = "invalid",
    ):
        super().__init__("diagnostic_invalid_structure", message)
        self.position = position
        self.object_count = object_count
        self.reason = reason


def _json(value, default):
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _state(session: DiagnosticSession) -> dict:
    state = _json(session.pending_items_json, {})
    state.setdefault("learned_point_ids", [])
    state.setdefault("unlearned_point_ids", [])
    state.setdefault("uncertain", [])
    state.setdefault("question_count", None)
    state.setdefault("learning_mode", "auto")
    return state


def _save_state(session: DiagnosticSession, state: dict) -> None:
    session.pending_items_json = json.dumps(state, ensure_ascii=False)
    session.updated_at = now_iso()


def _points(db: Session, source_id: str) -> list[KnowledgePoint]:
    return db.execute(
        select(KnowledgePoint)
        .where(KnowledgePoint.source_id == source_id, KnowledgePoint.status == "active")
        .order_by(KnowledgePoint.id)
    ).scalars().all()


def _session_source_specs(session: DiagnosticSession) -> list[dict]:
    if session.source_scope_json:
        try:
            specs = json.loads(session.source_scope_json)
        except (TypeError, json.JSONDecodeError):
            specs = []
        if isinstance(specs, list) and specs:
            return specs
    return [{"source_id": session.source_id, "page_start": None, "page_end": None}]


def _session_source_ids(session: DiagnosticSession) -> set[str]:
    return {
        spec.get("source_id")
        for spec in _session_source_specs(session)
        if isinstance(spec, dict) and spec.get("source_id")
    }


def _session_points(db: Session, session: DiagnosticSession) -> list[KnowledgePoint]:
    if session.knowledge_base_id and session.source_scope_json:
        specs = _session_source_specs(session)
        source_ids = [spec.get("source_id") for spec in specs if spec.get("source_id")]
        rows = db.execute(
            select(KnowledgePoint).where(
                KnowledgePoint.source_id.in_(source_ids),
                KnowledgePoint.status == "active",
            )
        ).scalars().all()
        return [
            point
            for point in rows
            if not any(marker in (point.display_name or "") for marker in META_POINT_MARKERS)
            and not is_fragment_point(point.display_name or "")
        ]
    return _points(db, session.source_id)


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


def is_meta_point(name: str) -> bool:
    return any(marker in (name or "") for marker in META_POINT_MARKERS)


MAX_TOPICS_PER_SOURCE = 6
_CHAPTER_HEADING_RE = re.compile(
    r"^第[一二三四五六七八九十百零\d]+[章节部分]\s*[:：]?\s*(.+)$"
)
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$")
_ENGLISH_HEADING_RE = re.compile(
    r"^(chapter|section|unit|part|lesson|module)\s+\d+(?:\.\d+)*\s*[:.\-]?\s*(.*)$",
    re.IGNORECASE,
)
_NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+){0,3})[\s、.:：\-]+(.+)$")
_GENERIC_CHAPTER_RE = re.compile(
    r"^(chapter|section|unit|part|lesson|module)\s+\d+(?:\.\d+)*$",
    re.IGNORECASE,
)


def _clean_topic(text: str | None) -> str:
    value = (text or "").strip().strip("#").strip(" ：:　-—")
    return value[:60].strip()


def _topic_is_usable(topic: str) -> bool:
    if not topic or not 2 <= len(topic) <= 60:
        return False
    if is_generic_point(topic) or is_meta_point(topic):
        return False
    if re.fullmatch(r"[\d\s.\-—–_]+", topic):
        return False
    if re.fullmatch(r"\d{1,2}/\d{1,2}", topic):
        return False
    if re.fullmatch(r".*\.(?:pdf|txt|md|png|jpe?g|webp)", topic, re.IGNORECASE):
        return False
    return True


def _heading_topic(line: str | None) -> str:
    text = (line or "").strip()
    if not text:
        return ""
    if _GENERIC_CHAPTER_RE.fullmatch(text):
        return ""
    candidate = ""
    match = _CHAPTER_HEADING_RE.match(text)
    if match:
        candidate = match.group(1)
    else:
        match = _MARKDOWN_HEADING_RE.match(text)
        if match:
            candidate = match.group(1)
        else:
            match = _ENGLISH_HEADING_RE.match(text)
            if match:
                candidate = match.group(2) or ""
            else:
                match = _NUMBERED_HEADING_RE.match(text)
                if match:
                    candidate = match.group(2)
    topic = _clean_topic(candidate)
    return topic if _topic_is_usable(topic) else ""


def _append_topic(
    topics: list[dict],
    seen: set[str],
    topic: str,
    page_start: int | None = None,
    page_end: int | None = None,
) -> None:
    normalized = normalize_point_name(topic)
    if not normalized or normalized in seen:
        return
    seen.add(normalized)
    topics.append(
        {"display_name": topic, "page_start": page_start, "page_end": page_end}
    )


def _topic_has_evidence(
    db: Session,
    source_id: str,
    page_start: int | None,
    page_end: int | None,
) -> bool:
    if db is None:
        return True
    query = select(func.count()).select_from(Evidence).where(
        Evidence.source_id == source_id,
        Evidence.status == "active",
    )
    if page_start is not None or page_end is not None:
        query = query.where(
            Evidence.page_no >= (page_start or 1),
            Evidence.page_no <= (page_end or 10_000),
        )
    return bool(db.execute(query).scalar_one())


def _chapter_topics(
    db: Session,
    source: SourceMaterial,
    page_start: int | None,
    page_end: int | None,
) -> list[dict]:
    page_count = int(source.page_count or 0)
    if page_count <= 0:
        return []
    chapters = chapters_for_source(db, source, page_count)
    topics: list[dict] = []
    seen: set[str] = set()
    for chapter in chapters:
        start = chapter.get("page_start")
        end = chapter.get("page_end")
        if start is None or end is None:
            continue
        if page_start is not None:
            start = max(start, page_start)
        if page_end is not None:
            end = min(end, page_end)
        if end < start:
            continue
        topic = _heading_topic(chapter.get("title"))
        if not topic:
            raw = _clean_topic(chapter.get("title"))
            if raw and not _GENERIC_CHAPTER_RE.fullmatch(raw) and _topic_is_usable(raw):
                topic = raw
        if not topic:
            continue
        if not _topic_has_evidence(db, source.id, start, end):
            continue
        _append_topic(topics, seen, topic, start, end)
    return topics


def _evidence_topics(
    db: Session,
    source: SourceMaterial,
    page_start: int | None,
    page_end: int | None,
) -> list[dict]:
    query = select(Evidence).where(
        Evidence.source_id == source.id,
        Evidence.status == "active",
        Evidence.kind == "text_span",
    )
    if source.kind == "pdf" and (page_start is not None or page_end is not None):
        query = query.where(
            Evidence.page_no >= (page_start or 1),
            Evidence.page_no <= (page_end or 10_000),
        )
    rows = db.execute(
        query.order_by(Evidence.page_no.is_(None), Evidence.page_no, Evidence.ordinal)
    ).scalars().all()
    topics: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        text = row.body_text or row.quote_text or ""
        for line in text.splitlines():
            topic = _heading_topic(line)
            if topic:
                _append_topic(topics, seen, topic, row.page_no, row.page_no)
    return topics


def _limit_topics(topics: list[dict]) -> list[dict]:
    limit = MAX_TOPICS_PER_SOURCE
    if len(topics) <= limit:
        return topics
    if limit <= 0:
        return []
    step = len(topics) / limit
    return [topics[min(len(topics) - 1, int(index * step))] for index in range(limit)]


def _skipped_chapter_topics(
    db: Session,
    source_id: str,
    page_start: int | None,
    page_end: int | None,
) -> list[dict]:
    source = db.get(SourceMaterial, source_id)
    if source is None or source.kind != "pdf" or not source.page_count:
        return []
    rows: list[dict] = []
    for chapter in chapters_for_source(db, source, source.page_count):
        start = chapter.get("page_start")
        end = chapter.get("page_end")
        if start is None or end is None:
            continue
        if page_start is not None:
            start = max(start, page_start)
        if page_end is not None:
            end = min(end, page_end)
        if end < start:
            continue
        topic = _heading_topic(chapter.get("title"))
        if not topic:
            raw = _clean_topic(chapter.get("title"))
            if raw and not _GENERIC_CHAPTER_RE.fullmatch(raw) and _topic_is_usable(raw):
                topic = raw
        if not topic or _topic_has_evidence(db, source.id, start, end):
            continue
        rows.append(
            {
                "display_name": topic,
                "source_id": source.id,
                "page_start": start,
                "page_end": end,
                "reason": "该页段没有可核对原文，未纳入计划",
            }
        )
    return rows


def _source_topics(
    db: Session,
    source_id: str,
    page_start: int | None = None,
    page_end: int | None = None,
) -> list[dict]:
    source = db.get(SourceMaterial, source_id)
    if source is None or source.status != "active":
        return []
    topics = _chapter_topics(db, source, page_start, page_end)
    if len(topics) < 2:
        topics.extend(_evidence_topics(db, source, page_start, page_end))
    merged: list[dict] = []
    seen: set[str] = set()
    for topic in topics:
        normalized = normalize_point_name(topic["display_name"])
        if normalized and normalized not in seen:
            seen.add(normalized)
            merged.append(topic)
    return _limit_topics(merged)


@dataclass
class DiagnosticRef:
    ref: str
    evidence_id: str
    kind: str
    page_no: int | None
    quote: str
    locator: dict
    source_id: str
    content_scope: str
    origin: str
    citation_id: str | None
    verifiable: bool
    ordinal: int = 0

    def as_citation(self) -> dict:
        return {
            "ref": self.ref,
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "page_no": self.page_no,
            "quote": self.quote,
            "locator": self.locator,
            "source_id": self.source_id,
            "content_scope": self.content_scope,
            "origin": self.origin,
            "citation_id": self.citation_id,
            "verifiable": self.verifiable,
            "id": self.citation_id,
            "claim": self.quote[:80],
        }


def _locator(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _ref_prefix(kind: str, page_no: int | None) -> str:
    if kind == "text_span":
        return "T"
    if kind == "page_image":
        return "P"
    return "I" if page_no is not None else "F"


def _diagnostic_ref(
    db: Session,
    evidence: Evidence,
    source: SourceMaterial,
    *,
    origin: str = "evidence",
    citation_id: str | None = None,
    content_scope: str | None = None,
) -> DiagnosticRef | None:
    if evidence.status != "active" or evidence.source_id != source.id:
        return None
    locator = _locator(evidence.locator_json)
    quote = (evidence.quote_text or evidence.label_text or "").strip()
    if evidence.kind == "text_span":
        if not quote:
            return None
        scope = "text"
    elif evidence.kind == "page_image":
        if evidence.page_no is None:
            return None
        page = db.get(SourcePage, {"source_id": source.id, "page_no": evidence.page_no})
        if page is None:
            return None
        quote = quote or f"第 {evidence.page_no} 页整页图像"
        scope = content_scope or "label_only"
    elif evidence.kind == "image_region":
        asset = db.get(ImageAsset, locator.get("asset_id")) if locator.get("asset_id") else None
        if asset is None or asset.source_id != source.id:
            return None
        if evidence.page_no is not None:
            page = db.get(SourcePage, {"source_id": source.id, "page_no": evidence.page_no})
            if page is None:
                return None
        quote = quote or "图中区域"
        scope = content_scope or "label_only"
    else:
        return None
    if not quote or len(quote) > 2000:
        return None
    if not locator:
        return None
    return DiagnosticRef(
        ref="",
        evidence_id=evidence.id,
        kind=evidence.kind,
        page_no=evidence.page_no,
        quote=quote[:200],
        locator=locator,
        source_id=source.id,
        content_scope=scope,
        origin=origin,
        citation_id=citation_id,
        verifiable=True,
        ordinal=evidence.ordinal,
    )


def _assign_refs(refs: list[DiagnosticRef]) -> list[DiagnosticRef]:
    counters = {"T": 0, "P": 0, "I": 0, "F": 0}
    seen: set[str] = set()
    result: list[DiagnosticRef] = []
    for item in sorted(
        refs,
        key=lambda row: (row.page_no is None, row.page_no or 0, row.ordinal, row.evidence_id),
    ):
        if item.evidence_id in seen:
            continue
        prefix = _ref_prefix(item.kind, item.page_no)
        counters[prefix] += 1
        item.ref = f"{prefix}{counters[prefix]}"
        result.append(item)
        seen.add(item.evidence_id)
    return result


def _whitelist_from_assembly(
    db: Session, assembly, *, images_sent: bool = False
) -> list[DiagnosticRef]:
    source_id = assembly.records[0]["source_id"] if assembly.records else None
    source = db.get(SourceMaterial, source_id) if source_id else None
    if source is None:
        return []
    refs: list[DiagnosticRef] = []
    for record in assembly.records:
        target = assembly.ref_map.targets.get(record["local_ref"])
        if target is None:
            continue
        evidence = db.get(Evidence, target.evidence_id)
        if evidence is None:
            continue
        item = _diagnostic_ref(
            db,
            evidence,
            source,
            content_scope="image_asset" if images_sent and evidence.kind != "text_span" else None,
        )
        if item is None:
            continue
        item.ordinal = record.get("ordinal") or item.ordinal
        refs.append(item)
    refs = _assign_refs(refs)
    text_refs = [item for item in refs if item.kind == "text_span"][:8]
    image_refs = [item for item in refs if item.kind != "text_span"][:8]
    combined = _assign_refs(text_refs + image_refs)
    return combined[:24]


def _citation_compat(
    db: Session, session: DiagnosticSession, refs: list[DiagnosticRef]
) -> list[DiagnosticRef]:
    if not refs:
        return refs
    seen = {item.evidence_id for item in refs}
    source = db.get(SourceMaterial, session.source_id)
    if source is None:
        return refs
    rows = db.execute(
        select(Citation)
        .where(Citation.source_id == session.source_id)
        .order_by(Citation.page_no, Citation.id)
        .limit(8)
    ).scalars().all()
    extra: list[DiagnosticRef] = []
    for row in rows:
        if row.evidence_id in seen:
            continue
        evidence = db.get(Evidence, row.evidence_id)
        if evidence is None:
            continue
        item = _diagnostic_ref(
            db, evidence, source, origin="citation", citation_id=row.id
        )
        if item is None:
            continue
        seen.add(item.evidence_id)
        extra.append(item)
    return _assign_refs(refs + extra)[:24]


def _freeze_whitelist(session: DiagnosticSession, refs: list[DiagnosticRef]) -> dict:
    state = _state(session)
    snapshot = {
        item.ref: {
            "evidence_id": item.evidence_id,
            "origin": item.origin,
            "citation_id": item.citation_id,
        }
        for item in refs
    }
    state["ref_whitelist"] = snapshot
    state["ref_whitelist_frozen_at"] = now_iso()
    _save_state(session, state)
    return snapshot


def _load_frozen(session: DiagnosticSession) -> dict | None:
    frozen = _state(session).get("ref_whitelist")
    return frozen if isinstance(frozen, dict) and frozen else None


def _hydrate_frozen(
    db: Session, session: DiagnosticSession, frozen: dict
) -> list[DiagnosticRef]:
    refs: list[DiagnosticRef] = []
    sources: dict[str, SourceMaterial] = {}
    for ref, row in frozen.items():
        evidence = db.get(Evidence, row.get("evidence_id"))
        if evidence is None:
            continue
        source = sources.get(evidence.source_id)
        if source is None:
            source = db.get(SourceMaterial, evidence.source_id)
            if source is None or source.status != "active":
                continue
            sources[evidence.source_id] = source
        item = _diagnostic_ref(
            db,
            evidence,
            source,
            origin=row.get("origin") or "evidence",
            citation_id=row.get("citation_id"),
        )
        if item is None:
            continue
        item.ref = ref
        refs.append(item)
    return refs


def _turn_context(
    db: Session, session: DiagnosticSession
) -> tuple[list[DiagnosticRef], dict | None]:
    frozen = _load_frozen(session)
    if frozen is not None:
        refs = _hydrate_frozen(db, session, frozen)
        if not refs:
            raise DiagnosisError(
                "diagnostic_insufficient_evidence", "本次资料没有可核对的证据"
            )
        return refs, None
    try:
        specs = _session_source_specs(session)
        query_terms = [session.target_goal or ""]
        query_terms.extend(point.display_name for point in _session_points(db, session))
        assembly = assemble_for_sources(
            db,
            specs,
            load_images=False,
            text_limit=settings.model_max_text_items,
            image_limit=settings.model_max_images,
            balanced=bool(session.knowledge_base_id or len(specs) > 1),
            query_terms=[term for term in query_terms if term],
        )
    except ValidationError as exc:
        raise DiagnosisError(
            "diagnostic_insufficient_evidence", exc.message
        ) from exc
    refs = _whitelist_from_assembly(db, assembly, images_sent=False)
    if not session.knowledge_base_id:
        refs = _citation_compat(db, session, refs)
    if not refs:
        raise DiagnosisError(
            "diagnostic_insufficient_evidence", "本次资料没有可核对的证据"
        )
    text_bytes = sum(len(item.description.encode("utf-8")) for item in assembly.text_items)
    image_bytes = sum(len(item.data) for item in assembly.images)
    locations = sorted(
        {
            (target.source_id, target.page_no)
            for target in assembly.ref_map.targets.values()
            if target.page_no is not None
        }
    )
    state = _state(session)
    state["last_context_stats"] = {
        "text_items": len(assembly.text_items),
        "images": len(assembly.images),
        "text_bytes": text_bytes,
        "image_bytes": image_bytes,
        "sources_pages": locations,
    }
    _save_state(session, state)
    logger.info(
        "diagnosis evidence stats session=%s text_items=%s images=%s bytes=%s sources_pages=%s",
        session.id,
        len(assembly.text_items),
        len(assembly.images),
        text_bytes + image_bytes,
        locations,
    )
    snapshot = _freeze_whitelist(session, refs)
    return refs, snapshot


def _diagnosis_refs(db: Session, session: DiagnosticSession | str) -> list[dict]:
    if isinstance(session, str):
        specs = [{"source_id": session, "page_start": None, "page_end": None}]
    else:
        specs = _session_source_specs(session)
    refs: list[DiagnosticRef] = []
    for spec in specs:
        source = db.get(SourceMaterial, spec.get("source_id"))
        if source is None or source.status != "active":
            continue
        query = select(Evidence).where(
            Evidence.source_id == source.id, Evidence.status == "active"
        )
        if source.kind == "pdf" and spec.get("page_start"):
            query = query.where(
                Evidence.page_no >= spec["page_start"], Evidence.page_no <= spec["page_end"]
            )
        rows = db.execute(
            query.order_by(Evidence.page_no.is_(None), Evidence.page_no, Evidence.ordinal).limit(24)
        ).scalars().all()
        for row in rows:
            item = _diagnostic_ref(db, row, source)
            if item is not None:
                refs.append(item)
    return [item.as_citation() for item in _assign_refs(refs)]


def _citation(db: Session, session: DiagnosticSession) -> list[dict]:
    frozen = _load_frozen(session)
    if frozen:
        return [
            {"ref": item.ref, "page_no": item.page_no, "evidence_id": item.evidence_id}
            for item in _hydrate_frozen(db, session, frozen)
        ]
    return _diagnosis_refs(db, session)


def _options(values: list[str]) -> list[dict]:
    return [{"key": chr(65 + index), "text": value} for index, value in enumerate(values)]


def _scope_spec(point: KnowledgePoint, source_id: str, page_no: int | None) -> dict:
    return {
        "kind": "scope",
        "intent": "scope",
        "stem": f"这份资料里的「{point.display_name}」，你目前的情况是？",
        "options": _options(
            [
                f"已学过「{point.display_name}」",
                f"只认识基础内容，还没学「{point.display_name}」",
                "不确定，先跳过",
            ]
        ),
        "answer_index": None,
        "explanation": None,
        "point": point,
        "citation": [{"id": None, "source_id": source_id, "page_no": page_no}],
    }


def _diagnostic_spec(point: KnowledgePoint, source_id: str, page_no: int | None) -> dict:
    return {
        "kind": "diagnostic",
        "intent": "check_understanding",
        "stem": f"根据资料，关于「{point.display_name}」，下面哪项判断成立？",
        "options": _options(
            [
                f"能按资料中的依据判断「{point.display_name}」",
                "只要换了设备数量就一定不成立",
                "不需要看资料，凭感觉即可",
                "与本次资料范围无关",
            ]
        ),
        "answer_index": 0,
        "explanation": f"资料中给出了「{point.display_name}」的判断依据，应据此判断，而不是凭设备数量或感觉。",
        "point": point,
        "citation": [{"id": None, "source_id": source_id, "page_no": page_no}],
    }


def _priority_spec(point: KnowledgePoint | None, source_id: str, page_no: int | None, wrong: bool) -> dict:
    name = point.display_name if point is not None else "上次错题"
    reason = "这次诊断题答错了" if wrong else "目前的把握还不够"
    return {
        "kind": "priority",
        "intent": "review_priority",
        "stem": f"{reason}。「{name}」这次怎么安排？",
        "options": _options(
            [
                "先看一段对照讲解，再做一道新题",
                "直接用不同情境的新题验证",
                "这轮先不安排，留到下次",
            ]
        ),
        "answer_index": None,
        "explanation": None,
        "point": point,
        "citation": [{"id": None, "source_id": source_id, "page_no": page_no}],
    }


def _turn_failure(
    session: DiagnosticSession,
    code: str,
    message: str,
    retryable: bool = True,
    *,
    whitelist_snapshot: dict | None = None,
) -> dict:
    state = _state(session)
    if whitelist_snapshot and "ref_whitelist" not in state:
        state["ref_whitelist"] = whitelist_snapshot
        state["ref_whitelist_frozen_at"] = now_iso()
    state["last_turn_error"] = {
        "round": session.current_round,
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    _save_state(session, state)
    return {"code": code, "message": message, "retryable": retryable}


def _turn_error(
    db: Session,
    session: DiagnosticSession,
    code: str,
    message: str,
    retryable: bool = True,
    *,
    whitelist_snapshot: dict | None = None,
):
    error = _turn_failure(
        session, code, message, retryable, whitelist_snapshot=whitelist_snapshot
    )
    db.commit()
    raise DiagnosisError(error["code"], error["message"])


def _generation_error_code(code: str) -> str:
    return {
        "no_api_key": "diagnostic_no_api_key",
        "auth_failed": "diagnostic_auth_failed",
        "generation_timeout": "diagnostic_generation_timeout",
        "insufficient_evidence": "diagnostic_insufficient_evidence",
        "request_too_large": "diagnostic_request_too_large",
        "schema_invalid": "diagnostic_invalid_structure",
    }.get(code, code or "diagnostic_model_error")


_CODE_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


@dataclass
class ParsedDiagnosisPayload:
    data: dict
    object_count: int
    used_fence: bool


def _scan_json_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            data, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise DiagnosisParseError(
                "模型返回内容不是合法 JSON",
                position=exc.pos,
                object_count=len(objects),
                reason="invalid_json",
            ) from exc
        if not isinstance(data, dict):
            raise DiagnosisParseError(
                "模型返回内容不是 JSON 对象",
                position=index,
                object_count=len(objects),
                reason="not_object",
            )
        objects.append(data)
        index = end
    if not objects:
        raise DiagnosisParseError(
            "模型返回内容不包含 JSON 对象",
            position=0,
            object_count=0,
            reason="empty_objects",
        )
    return objects


def _parse_json_payload(text: str | None) -> ParsedDiagnosisPayload:
    raw = (text or "").strip()
    if not raw:
        raise DiagnosisParseError(
            "模型返回内容为空",
            position=0,
            object_count=0,
            reason="empty",
        )
    candidates: list[tuple[str, bool, int]] = [(raw, False, 0)]
    fenced = _CODE_FENCE.match(raw)
    if fenced:
        candidates.append((fenced.group(1), True, fenced.start(1)))
    last_error: DiagnosisParseError | None = None
    for candidate, used_fence, offset in candidates:
        if not candidate.strip():
            last_error = DiagnosisParseError(
                "模型返回内容为空",
                position=offset,
                object_count=0,
                reason="empty",
            )
            continue
        try:
            objects = _scan_json_objects(candidate)
        except DiagnosisParseError as exc:
            if offset and exc.position is not None:
                exc.position += offset
            last_error = exc
            continue
        return ParsedDiagnosisPayload(
            data=objects[0],
            object_count=len(objects),
            used_fence=used_fence,
        )
    if last_error is not None:
        raise last_error
    raise DiagnosisParseError(
        "模型返回内容不是合法 JSON",
        position=0,
        object_count=0,
        reason="invalid",
    )


def _parse_json_object(text: str | None) -> dict:
    return _parse_json_payload(text).data


def _normalize_refs_used(data: dict) -> list[str]:
    raw = (
        data.get("citation_ref")
        or data.get("evidence_refs")
        or data.get("citations")
        or []
    )
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raw = []
    values: list[str] = []
    for item in raw:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get("ref") or item.get("local_ref") or item.get("citation_ref")
        else:
            value = None
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _normalize_ref_token(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    pairs = (
        ("[", "]"),
        ("【", "】"),
        ("(", ")"),
        ("（", "）"),
        ("{", "}"),
        ("<", ">"),
        ("「", "」"),
        ("『", "』"),
        ('"', '"'),
        ("'", "'"),
        ("`", "`"),
    )
    for _ in range(4):
        previous = text
        text = text.strip()
        for left, right in pairs:
            if (
                len(text) > len(left) + len(right)
                and text.startswith(left)
                and text.endswith(right)
            ):
                text = text[len(left) : -len(right)]
                break
        text = text.rstrip("。，,.;；:：!！?？").strip()
        if text == previous:
            break
    match = re.fullmatch(r"([TPIF])([1-9][0-9]*)", text, re.IGNORECASE)
    if match is None:
        return None
    return f"{match.group(1).upper()}{match.group(2)}"


def _validate_refs(
    refs_used: list[str],
    refs_by_name: dict[str, DiagnosticRef],
    session: DiagnosticSession,
) -> list[DiagnosticRef]:
    if not refs_used:
        raise DiagnosisError("diagnostic_citation_out_of_scope", "本题缺少可核对的引用依据")
    resolved: list[DiagnosticRef] = []
    allowed_sources = _session_source_ids(session)
    allowed = "、".join(sorted(refs_by_name)[:12]) or "（无）"
    for raw_ref in refs_used:
        ref = _normalize_ref_token(raw_ref)
        if ref is None:
            raise DiagnosisError(
                "diagnostic_citation_out_of_scope",
                f"引用编号格式不合法：{raw_ref}。请只使用允许的编号（如 T1），不要加方括号、标点或自行编号",
            )
        item = refs_by_name.get(ref)
        if item is None or not item.verifiable:
            raise DiagnosisError(
                "diagnostic_citation_out_of_scope",
                f"引用不在本次资料范围内：{raw_ref}。请从允许的编号中选择：{allowed}",
            )
        if item.source_id not in allowed_sources:
            raise DiagnosisError("diagnostic_citation_out_of_scope", "引用来自本次范围外的资料")
        if item not in resolved:
            resolved.append(item)
    return resolved


def _reject_unseen_image_question(kind: str, refs: list[DiagnosticRef]) -> bool:
    if kind != "diagnostic" or not refs:
        return False
    return all(
        item.kind in {"page_image", "image_region"}
        and item.content_scope == "label_only"
        for item in refs
    )


def _model_spec(db: Session, session: DiagnosticSession, client=None) -> dict | None:
    snapshot = None
    try:
        refs, snapshot = _turn_context(db, session)
        refs_by_name = {item.ref: item for item in refs}
        points = _session_points(db, session)
        answers = db.execute(
            select(DiagnosticAnswer, DiagnosticQuestion)
            .join(DiagnosticQuestion, DiagnosticQuestion.id == DiagnosticAnswer.question_id)
            .where(DiagnosticAnswer.session_id == session.id)
            .order_by(DiagnosticAnswer.created_at)
        ).all()
        history = "\n".join(
            f"{index}. [{question.kind}] {question.stem} -> {answer.selected_index} ({answer.status})"
            for index, (answer, question) in enumerate(answers, start=1)
        ) or "（尚无回答）"
        text = "\n".join(
            f"{item.ref} | kind={item.kind} | page={item.page_no} | scope={item.content_scope} | {item.quote}"
            for item in refs
        )
        base_task = (
            "你是循知的学习诊断问题生成器。只输出一个 JSON 对象，不要输出多余文字、数组、多个对象或 Markdown 代码围栏。\n"
            "即使【本次目标】包含多个主题，每轮也只选择一个主题，只输出一道题和一个 JSON 对象；其余主题留给后续诊断。\n"
            "字段：question_type（scope|focus|probe_question|goal）、stem、options（2～4 个字符串）、"
            "answer_index（probe_question 必填）、explanation（probe_question 必填）、knowledge_point（可选）、"
            "citation_ref（必填，只能使用给定编号；也可用 citations 数组）。\n"
            "单题示例（编号仅示例，必须换成可用引用中的编号）："
            '{"question_type":"scope","stem":"你学过 IPv4 子网划分吗？","options":["学过","没学过"],"citation_ref":"T1"}\n'
            f"本次目标：{session.target_goal or '未填写目标'}\n"
            f"已确认范围提示：{json.dumps(_state(session), ensure_ascii=False)[:800]}\n"
            f"历史问答：{history}\n"
            f"可用引用：\n{text}\n"
            "引用规则：每个问题至少给出 1 条命中编号的 citation_ref；提出新考点必须给出能支持它的 citation_ref；"
            "只能逐字使用「可用引用」每行左侧的编号（如 T1），不要加方括号、标点、空格或自行改号；"
            "不得跨资料合并同名考点。\n"
            "图像规则：本次没有把图像内容发送给模型，P/I/F 只是文件名、页码或区域标签；"
            "可以询问学习范围（scope/focus/goal），但不要生成需要看图才能判分的 probe_question。"
        )
        if client is None:
            client = get_model_client()
        data = None
        object_count = 0
        used_fence = False
        last_parse_error: DiagnosisParseError | None = None
        for attempt in (1, 2):
            task = base_task
            if attempt == 2:
                task += (
                    "\n【格式纠正】上一次响应无法解析为单个完整 JSON 对象。"
                    "请只输出一道题、一个 JSON 对象；不要输出数组、多个对象、第二个对象、解释文字或 Markdown 代码围栏；"
                    "所有字段必须在同一个对象内完整闭合。"
                )
            request = ModelRequest(
                system_prompt="你是循知的学习诊断助手，只输出合法 JSON。",
                task_text=task,
                text_items=[TextPayload(ref="REF", description=text)],
                question_count=1,
            )
            try:
                result = client.generate(request)
            except ModelError as exc:
                raise DiagnosisError(
                    _generation_error_code(exc.code), exc.message
                ) from exc
            request_stats = dict(getattr(result, "request_stats", {}) or {})
            if request_stats:
                state = _state(session)
                context_stats = dict(state.get("last_context_stats") or {})
                context_stats["request"] = request_stats
                state["last_context_stats"] = context_stats
                _save_state(session, state)
                logger.info(
                    "diagnosis request sent session=%s attempt=%s %s",
                    session.id,
                    attempt,
                    request_stats,
                )
            finish_reason = getattr(result, "finish_reason", None)
            content = getattr(result, "content_text", "") or ""
            if finish_reason == "length":
                parse_error = DiagnosisParseError(
                    "模型返回内容被截断",
                    position=len(content),
                    object_count=0,
                    reason="finish_length",
                )
            else:
                try:
                    parsed = _parse_json_payload(content)
                except DiagnosisParseError as exc:
                    parse_error = exc
                else:
                    data = parsed.data
                    object_count = parsed.object_count
                    used_fence = parsed.used_fence
                    logger.info(
                        "diagnosis parse ok session=%s attempt=%s finish_reason=%s length=%s objects=%s used=first fence=%s recovered=%s",
                        session.id,
                        attempt,
                        finish_reason or "stop",
                        len(content),
                        object_count,
                        used_fence,
                        object_count > 1,
                    )
                    break
            last_parse_error = parse_error
            logger.warning(
                "diagnosis parse failed session=%s attempt=%s finish_reason=%s length=%s objects=%s position=%s reason=%s",
                session.id,
                attempt,
                finish_reason or "unknown",
                len(content),
                parse_error.object_count,
                parse_error.position,
                parse_error.reason,
            )
            if attempt == 2:
                raise parse_error
        if data is None:
            raise last_parse_error or DiagnosisParseError(
                "模型返回内容不是合法 JSON",
                position=0,
                object_count=0,
                reason="invalid",
            )
        kind_map = {
            "scope": "scope",
            "focus": "priority",
            "probe_question": "diagnostic",
            "goal": "goal",
        }
        raw_kind = data.get("question_type")
        if raw_kind not in kind_map:
            raise DiagnosisError("diagnostic_invalid_structure", "模型返回的题型不在允许范围内")
        kind = kind_map[raw_kind]
        stem = str(data.get("stem") or "").strip()
        options = data.get("options") or []
        if not stem or not isinstance(options, list) or not 2 <= len(options) <= 4:
            raise DiagnosisError("diagnostic_invalid_structure", "模型返回的问题缺少题干或选项")
        option_texts = [str(value).strip() for value in options]
        if len(set(option_texts)) != len(option_texts):
            raise DiagnosisError("diagnostic_invalid_option", "模型返回的选项存在重复")
        option_rows = _options(option_texts)
        answer_index = data.get("answer_index") if kind == "diagnostic" else None
        if kind == "diagnostic" and (
            not isinstance(answer_index, int) or not 0 <= answer_index < len(option_rows)
        ):
            raise DiagnosisError("diagnostic_invalid_option", "诊断小题缺少可校验的正确答案")
        refs_used = _normalize_refs_used(data)
        resolved = _validate_refs(refs_used, refs_by_name, session)
        if _reject_unseen_image_question(kind, resolved):
            raise DiagnosisError(
                "diagnostic_invalid_structure",
                "图像内容未送达模型，本题不可判分",
            )
        point = None
        point_name = str(data.get("knowledge_point") or "").strip()
        if point_name:
            point = find_knowledge_point(db, session.source_id, point_name)
            if point is None:
                point = ensure_knowledge_point(
                    db, session.source_id, point_name, status="candidate"
                )
        state = _state(session)
        state.pop("last_turn_error", None)
        _save_state(session, state)
        return {
            "kind": kind,
            "intent": raw_kind,
            "stem": stem,
            "options": option_rows,
            "answer_index": answer_index,
            "explanation": str(data.get("explanation") or "") or None,
            "point": point,
            "citation": resolved,
        }
    except DiagnosisError as exc:
        if snapshot is not None and getattr(exc, "whitelist_snapshot", None) is None:
            exc.whitelist_snapshot = snapshot
        raise


def retry_turn(db: Session, session: DiagnosticSession, client=None) -> dict:
    if session.status not in {"active", "preview"}:
        raise DiagnosisError("session_closed", "诊断已结束，不能重试本轮")
    question = db.execute(
        select(DiagnosticQuestion)
        .where(
            DiagnosticQuestion.session_id == session.id,
            DiagnosticQuestion.status == "active",
        )
        .order_by(DiagnosticQuestion.created_at.desc(), DiagnosticQuestion.id.desc())
    ).scalars().first()
    if question is not None:
        answered = db.execute(
            select(DiagnosticAnswer).where(
                DiagnosticAnswer.session_id == session.id,
                DiagnosticAnswer.question_id == question.id,
            )
        ).scalars().first()
        if answered is None:
            return session_payload(db, session)
    spec = None
    try:
        spec = _next_spec(db, session, client)
        if spec is not None:
            _create_round(db, session, spec)
        session.status = "active"
        state = _state(session)
        state.pop("last_turn_error", None)
        _save_state(session, state)
        db.commit()
    except DiagnosisError as exc:
        db.rollback()
        db.refresh(session)
        error = _turn_failure(
            session,
            exc.code,
            exc.message,
            whitelist_snapshot=getattr(exc, "whitelist_snapshot", None),
        )
        db.commit()
        return {**session_payload(db, session), "error": error}
    return session_payload(db, session)


def _goal_spec(source_id: str, page_no: int | None) -> dict:
    return {
        "kind": "goal",
        "intent": "clarify_goal",
        "stem": "这次最想先解决什么？",
        "options": _options(["理解一个新主题", "补上上次的薄弱点", "做一次考前复习"]),
        "answer_index": None,
        "explanation": None,
        "point": None,
        "citation": [{"id": None, "source_id": source_id, "page_no": page_no}],
    }


def _latest_answer(db: Session, session_id: str) -> DiagnosticAnswer | None:
    return db.execute(
        select(DiagnosticAnswer)
        .where(DiagnosticAnswer.session_id == session_id, DiagnosticAnswer.status != "superseded")
        .order_by(DiagnosticAnswer.created_at.desc(), DiagnosticAnswer.id.desc())
    ).scalars().first()


def decide_intent(db: Session, session: DiagnosticSession) -> dict | None:
    points = _session_points(db, session)
    page_no = None
    citation = _citation(db, session)
    if citation:
        page_no = citation[0].get("page_no")
    if not points:
        return _goal_spec(session.source_id, page_no)
    if session.current_round >= MAX_ROUNDS:
        return None
    last = _latest_answer(db, session.id)
    if last is None:
        return _scope_spec(points[0], session.source_id, page_no)
    question = db.get(DiagnosticQuestion, last.question_id)
    if question is None:
        return _goal_spec(session.source_id, page_no)
    if question.kind == "scope":
        if last.selected_index == 0:
            return _diagnostic_spec(question.knowledge_point_id and db.get(KnowledgePoint, question.knowledge_point_id) or points[0], session.source_id, page_no)
        if last.selected_index == 1:
            remaining = [item for item in points if item.id != question.knowledge_point_id]
            if remaining:
                return _scope_spec(remaining[0], session.source_id, page_no)
            return _goal_spec(session.source_id, page_no)
        return _priority_spec(
            db.get(KnowledgePoint, question.knowledge_point_id) if question.knowledge_point_id else None,
            session.source_id,
            page_no,
            False,
        )
    if question.kind == "diagnostic":
        point = db.get(KnowledgePoint, question.knowledge_point_id) if question.knowledge_point_id else None
        if last.is_correct is False:
            return _priority_spec(point, session.source_id, page_no, True)
        if session.current_round < DEFAULT_ROUNDS:
            if not (session.target_goal or "").strip():
                return _goal_spec(session.source_id, page_no)
            return _priority_spec(point, session.source_id, page_no, False)
        return None
    if question.kind == "priority":
        if not (session.target_goal or "").strip():
            return _goal_spec(session.source_id, page_no)
        return None
    return None


def build_turn(db: Session, session: DiagnosticSession, intent: dict | None, client=None) -> dict | None:
    if client is None:
        client = get_model_client()
    if is_fake_client(client):
        return intent
    return _model_spec(db, session, client)


def _next_spec(db: Session, session: DiagnosticSession, client=None) -> dict | None:
    return build_turn(db, session, decide_intent(db, session), client)


def _create_round(db: Session, session: DiagnosticSession, spec: dict) -> DiagnosticQuestion:
    ordinal = session.current_round + 1
    round_row = DiagnosticRound(
        id=new_id("diagnosis_round"),
        session_id=session.id,
        ordinal=ordinal,
        intent=spec["intent"],
        status="active",
        created_at=now_iso(),
    )
    db.add(round_row)
    db.flush()
    point = spec.get("point")
    question = DiagnosticQuestion(
        id=new_id("diagnosis_question"),
        session_id=session.id,
        round_id=round_row.id,
        ordinal=1,
        kind=spec["kind"],
        stem=spec["stem"],
        options_json=json.dumps(spec["options"], ensure_ascii=False),
        answer_index=spec.get("answer_index"),
        explanation=spec.get("explanation"),
        knowledge_point_id=point.id if point is not None else None,
        knowledge_point_name=point.display_name if point is not None else "",
        citation_json=json.dumps(
            [
                item.as_citation() if isinstance(item, DiagnosticRef) else item
                for item in (spec.get("citation") or [])
            ],
            ensure_ascii=False,
        ),
        status="active",
        created_at=now_iso(),
    )
    db.add(question)
    session.current_round = ordinal
    session.updated_at = now_iso()
    db.flush()
    return question


def _question_payload(question: DiagnosticQuestion, *, reveal: bool = False) -> dict:
    payload = {
        "id": question.id,
        "round": None,
        "kind": question.kind,
        "stem": question.stem,
        "options": _json(question.options_json, []),
        "knowledge_point_id": question.knowledge_point_id,
        "knowledge_point_name": question.knowledge_point_name,
        "citations": _json(question.citation_json, []),
        "status": question.status,
    }
    if reveal:
        payload["answer_index"] = question.answer_index
        payload["explanation"] = question.explanation
    return payload


def _documented_answer(db: Session, answer: DiagnosticAnswer) -> dict:
    question = db.get(DiagnosticQuestion, answer.question_id)
    return {
        "id": answer.id,
        "question_id": answer.question_id,
        "kind": question.kind if question is not None else "unknown",
        "stem": question.stem if question is not None else "",
        "selected_index": answer.selected_index,
        "status": answer.status,
        "is_correct": answer.is_correct,
        "knowledge_point_id": question.knowledge_point_id if question is not None else None,
        "knowledge_point_name": question.knowledge_point_name if question is not None else "",
    }


def session_payload(db: Session, session: DiagnosticSession) -> dict:
    answers = db.execute(
        select(DiagnosticAnswer)
        .where(DiagnosticAnswer.session_id == session.id, DiagnosticAnswer.status != "superseded")
        .order_by(DiagnosticAnswer.created_at, DiagnosticAnswer.id)
    ).scalars().all()

    question = db.execute(
        select(DiagnosticQuestion)
        .where(
            DiagnosticQuestion.session_id == session.id,
            DiagnosticQuestion.status == "active",
        )
        .order_by(DiagnosticQuestion.created_at.desc(), DiagnosticQuestion.id.desc())
    ).scalars().first()
    current = None
    if question is not None:
        answered = any(item.question_id == question.id for item in answers)
        if not answered and session.status == "active":
            current = _question_payload(question)
    state = _state(session)
    return {
        "id": session.id,
        "source_id": session.source_id,
        "status": session.status,
        "current_round": session.current_round,
        "goal": session.target_goal,
        "rule_version": DIAGNOSIS_RULE_VERSION,
        "current_question": current,
        "confirmed_info": [_documented_answer(db, item) for item in answers],
        "pending_items": state.get("uncertain") or [],
        "unlearned_point_ids": state.get("unlearned_point_ids") or [],
        "plan_version_id": session.plan_version_id,
        "knowledge_base_id": session.knowledge_base_id,
        "source_scope": _session_source_specs(session) if session.knowledge_base_id else [],
        "can_finish": session.status in {"active", "preview"}
        and (question is not None or bool(answers)),
        "question_count": state.get("question_count"),
        "learning_mode": state.get("learning_mode") or "auto",
        "schedule_options": state.get("schedule_options"),
        "last_turn_error": state.get("last_turn_error"),
        "generation": _generation_view(db, session),
    }


def start_session(
    db: Session,
    source: SourceMaterial,
    *,
    goal: str,
    question_count: int | None,
    learning_mode: str = "auto",
    knowledge_base_id: str | None = None,
    source_scope: list[dict] | None = None,
    session_key: str,
    direct_view: bool = False,
    client=None,
) -> dict:
    existing = db.execute(
        select(DiagnosticSession).where(DiagnosticSession.session_key == session_key)
    ).scalars().first()
    if existing is not None:
        return {"session": session_payload(db, existing), "reused": True}
    session = DiagnosticSession(
        id=new_id("diagnosis_session"),
        source_id=source.id,
        target_goal=(goal or "").strip(),
        status="active",
        current_round=0,
        session_key=session_key,
        knowledge_base_id=knowledge_base_id,
        source_scope_json=json.dumps(source_scope or [], ensure_ascii=False),
        pending_items_json=json.dumps(
            {
                "learned_point_ids": [],
                "unlearned_point_ids": [],
                "uncertain": [],
                "question_count": question_count if question_count in {3, 5, 8} else None,
                "learning_mode": learning_mode or "auto",
            },
            ensure_ascii=False,
        ),
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    db.add(session)
    db.flush()
    db.commit()
    if direct_view:
        session.status = "preview"
        db.commit()
    else:
        try:
            spec = _next_spec(db, session, client)
        except DiagnosisError as exc:
            db.rollback()
            db.refresh(session)
            error = _turn_failure(
            session,
            exc.code,
            exc.message,
            whitelist_snapshot=getattr(exc, "whitelist_snapshot", None),
        )
            db.commit()
            return {
                "session": session_payload(db, session),
                "reused": False,
                "error": error,
            }
        if spec is not None:
            _create_round(db, session, spec)
            db.commit()
    return {"session": session_payload(db, session), "reused": False}


def _apply_answer_state(db: Session, session: DiagnosticSession, question: DiagnosticQuestion, answer: DiagnosticAnswer) -> None:
    state = _state(session)
    if question.kind == "scope":
        option_text = ""
        options = _json(question.options_json, [])
        if answer.selected_index is not None and 0 <= answer.selected_index < len(options):
            option_text = options[answer.selected_index].get("text", "")
        if answer.selected_index == 0 and question.knowledge_point_id:
            state["learned_point_ids"].append(question.knowledge_point_id)
            if question.knowledge_point_id in state["unlearned_point_ids"]:
                state["unlearned_point_ids"].remove(question.knowledge_point_id)
        elif answer.selected_index == 1 and question.knowledge_point_id:
            state["unlearned_point_ids"].append(question.knowledge_point_id)
            if question.knowledge_point_id in state["learned_point_ids"]:
                state["learned_point_ids"].remove(question.knowledge_point_id)
        elif answer.selected_index == 2:
            state["uncertain"].append(
                {
                    "question_id": question.id,
                    "knowledge_point_id": question.knowledge_point_id,
                    "display_name": question.knowledge_point_name,
                    "reason": "学生选择不确定，保留为待确认。",
                }
            )

    if answer.status in {"skipped", "uncertain"}:
        state["uncertain"].append(
            {
                "question_id": question.id,
                "knowledge_point_id": question.knowledge_point_id,
                "display_name": question.knowledge_point_name,
                "reason": "未作答或选择跳过，保留为待确认。",
            }
        )
    _save_state(session, state)


def submit_answer(
    db: Session,
    session: DiagnosticSession,
    question_id: str,
    *,
    selected_index: int | None,
    status: str,
    answer_key: str,
    client=None,
) -> dict:
    if session.status not in {"active", "preview"}:
        raise DiagnosisError("session_closed", "诊断已结束，不能继续作答")
    question = db.get(DiagnosticQuestion, question_id)
    if question is None or question.session_id != session.id:
        raise DiagnosisError("question_not_found", "诊断问题不存在")
    if question.status != "active":
        raise DiagnosisError("question_superseded", "该问题已失效，请重新回答当前问题")
    existing = db.execute(
        select(DiagnosticAnswer).where(DiagnosticAnswer.answer_key == answer_key)
    ).scalars().first()
    if existing is not None:
        return session_payload(db, session)
    if status not in {"answered", "skipped", "uncertain"}:
        raise DiagnosisError("diagnostic_answer_invalid", "无效的作答状态")
    if status == "answered":
        options = _json(question.options_json, [])
        if selected_index is None or selected_index < 0 or selected_index >= len(options):
            raise DiagnosisError("diagnostic_answer_invalid", "请选择一个有效选项")
    answer = db.execute(
        select(DiagnosticAnswer).where(
            DiagnosticAnswer.session_id == session.id,
            DiagnosticAnswer.question_id == question.id,
        )
    ).scalars().first()
    if answer is None:
        answer = DiagnosticAnswer(
            id=new_id("diagnosis_answer"),
            session_id=session.id,
            question_id=question.id,
            created_at=now_iso(),
        )
        db.add(answer)
    answer.selected_index = selected_index
    answer.status = status
    answer.answer_key = answer_key
    if question.kind == "diagnostic" and status == "answered":
        answer.is_correct = selected_index == question.answer_index
    else:
        answer.is_correct = None
    answer.updated_at = now_iso()
    db.flush()
    _apply_answer_state(db, session, question, answer)
    # T1'：先提交答案与作答状态，生成失败也不会丢失已答内容。
    db.commit()

    try:
        if session.current_round >= MAX_ROUNDS or (question.kind == "diagnostic" and answer.is_correct is True and session.current_round >= DEFAULT_ROUNDS):
            session.status = "preview"
        else:
            spec = _next_spec(db, session, client)
            if spec is None:
                session.status = "preview"
            else:
                _create_round(db, session, spec)
        state = _state(session)
        state.pop("last_turn_error", None)
        _save_state(session, state)
        db.commit()
    except DiagnosisError as exc:
        db.rollback()
        db.refresh(session)
        error = _turn_failure(
            session,
            exc.code,
            exc.message,
            whitelist_snapshot=getattr(exc, "whitelist_snapshot", None),
        )
        db.commit()
        return {**session_payload(db, session), "error": error}
    return session_payload(db, session)


def reopen_answer(db: Session, session: DiagnosticSession, answer_id: str, client=None) -> dict:
    if session.status == "confirmed":
        raise DiagnosisError("session_confirmed", "诊断已确认，不能回改")
    answer = db.get(DiagnosticAnswer, answer_id)
    if answer is None or answer.session_id != session.id:
        raise DiagnosisError("answer_not_found", "作答记录不存在")
    question = db.get(DiagnosticQuestion, answer.question_id)
    if question is None:
        raise DiagnosisError("question_not_found", "诊断问题不存在")
    round_row = db.get(DiagnosticRound, question.round_id)
    if round_row is None:
        raise DiagnosisError("round_not_found", "诊断轮次不存在")
    ordinal = round_row.ordinal
    dependent_rounds = db.execute(
        select(DiagnosticRound).where(
            DiagnosticRound.session_id == session.id,
            DiagnosticRound.ordinal >= ordinal,
        )
    ).scalars().all()
    for item in dependent_rounds:
        db.query(DiagnosticAnswer).filter(DiagnosticAnswer.question_id.in_(
            select(DiagnosticQuestion.id).where(DiagnosticQuestion.round_id == item.id)
        )).delete(synchronize_session=False)
        db.query(DiagnosticQuestion).filter(DiagnosticQuestion.round_id == item.id).delete(synchronize_session=False)
        db.delete(item)
    session.current_round = ordinal - 1
    session.status = "active"
    session.plan_version_id = None
    state = _state(session)
    state["uncertain"] = [item for item in state["uncertain"] if item.get("question_id") != answer.question_id]
    _save_state(session, state)
    db.flush()
    # T1'：回改的删除结果先提交；即使后续生成失败，也不复活已删除轮次。
    db.commit()
    try:
        spec = _next_spec(db, session, client)
        if spec is not None:
            _create_round(db, session, spec)
        state = _state(session)
        state.pop("last_turn_error", None)
        _save_state(session, state)
        db.commit()
    except DiagnosisError as exc:
        db.rollback()
        db.refresh(session)
        error = _turn_failure(
            session,
            exc.code,
            exc.message,
            whitelist_snapshot=getattr(exc, "whitelist_snapshot", None),
        )
        db.commit()
        return {**session_payload(db, session), "error": error}
    return session_payload(db, session)


def _wrong_points(db: Session, session: DiagnosticSession) -> list[tuple[str, str, str]]:
    rows = db.execute(
        select(DiagnosticAnswer, DiagnosticQuestion)
        .join(DiagnosticQuestion, DiagnosticQuestion.id == DiagnosticAnswer.question_id)
        .where(
            DiagnosticAnswer.session_id == session.id,
            DiagnosticAnswer.is_correct.is_(False),
            DiagnosticQuestion.status == "active",
        )
    ).all()
    result = []
    for answer, question in rows:
        if question.knowledge_point_id:
            result.append((question.knowledge_point_id, answer.id, question.id))
    return result


def replan_session(
    db: Session,
    session: DiagnosticSession,
    *,
    goal: str | None = None,
    learning_mode: str | None = None,
) -> dict:
    if session.status != "confirmed":
        raise DiagnosisError(
            "session_not_confirmed",
            "只有已确认的诊断会话可以重新制定学习计划",
        )
    state = _state(session)
    previous_status = session.status
    session.status = "preview"
    session.updated_at = now_iso()
    try:
        result = build_plan_preview(
            db,
            session,
            goal=goal or session.target_goal,
            learning_mode=learning_mode or state.get("learning_mode") or "auto",
            force_new=True,
        )
    except Exception:
        db.rollback()
        fresh = db.get(DiagnosticSession, session.id)
        if fresh is not None and fresh.status != "confirmed":
            fresh.status = previous_status
            fresh.updated_at = now_iso()
            db.commit()
        raise
    return result


def _apply_diagnostic_basis(db: Session, version: LearningPlanVersion, session: DiagnosticSession) -> None:
    wrong = _wrong_points(db, session)
    if not wrong:
        return
    items = _json(version.items_json, [])
    adjustments = _json(version.adjustment_json, [])
    by_id = {item.get("knowledge_point_id"): item for item in items}
    total = version.question_count or 0
    changed = False
    for point_id, answer_id, question_id in wrong:
        item = by_id.get(point_id)
        if item is None:
            continue
        if any(
            entry.get("knowledge_point_id") == point_id and entry.get("basis_type") == "diagnostic_wrong"
            for entry in adjustments
        ):
            continue
        previous = item.get("suggested_question_count") or 0
        item["suggested_question_count"] = previous + 1
        item["priority"] = "needs_reinforce"
        item.setdefault("basis", []).append(
            {
                "type": "diagnostic_wrong",
                "diagnostic_session_id": session.id,
                "diagnostic_answer_id": answer_id,
                "diagnostic_question_id": question_id,
                "knowledge_point_id": point_id,
            }
        )
        adjustments.append(
            {
                "knowledge_point_id": point_id,
                "display_name": item.get("display_name") or "",
                "previous_question_count": previous,
                "current_question_count": previous + 1,
                "previous_priority": item.get("priority") or "review",
                "current_priority": "needs_reinforce",
                "reason": "这次诊断题答错，建议本轮用一道不同情境的新题验证；掌握状态仍按正式练习记录。",
                "basis_type": "diagnostic_wrong",
                "diagnostic_session_id": session.id,
                "evidence_refs": [
                    {
                        "type": "diagnostic_answer",
                        "diagnostic_answer_id": answer_id,
                        "diagnostic_question_id": question_id,
                        "knowledge_point_id": point_id,
                    }
                ],
            }
        )
        changed = True
    if not changed:
        return
    counts = sum(item.get("suggested_question_count") or 0 for item in items)
    scope = json.loads(version.scope_json or "{}")
    if not scope.get("auto_plan"):
        while total and counts > total:
            candidate = next(
                (
                    item
                    for item in reversed(items)
                    if (item.get("suggested_question_count") or 0) > 0
                    and item.get("priority") not in {"needs_reinforce"}
                ),
                None,
            )
            if candidate is None:
                break
            candidate["suggested_question_count"] -= 1
            counts -= 1
        version.question_count = total
    else:
        version.question_count = counts
    version.items_json = json.dumps(items, ensure_ascii=False)
    version.adjustment_json = json.dumps(adjustments, ensure_ascii=False)
    plan_service.refresh_module_counts(version)


def _prepare_source_points(
    db: Session,
    source_id: str,
    goal: str | None,
    *,
    page_start: int | None = None,
    page_end: int | None = None,
) -> list[KnowledgePoint]:
    candidates = db.execute(
        select(KnowledgePoint).where(
            KnowledgePoint.source_id == source_id,
            KnowledgePoint.status == "candidate",
        )
    ).scalars().all()
    for point in candidates:
        if (
            point.display_name
            and not is_generic_point(point.display_name)
            and not is_meta_point(point.display_name)
            and not is_fragment_point(point.display_name)
        ):
            point.status = "active"
            point.updated_at = now_iso()
    for point in _points(db, source_id):
        if is_meta_point(point.display_name) or is_fragment_point(point.display_name or ""):
            point.status = "candidate"
            point.updated_at = now_iso()
    db.flush()
    for topic in _source_topics(db, source_id, page_start, page_end):
        ensure_knowledge_point(
            db,
            source_id,
            topic["display_name"],
            status="active",
            page_start=topic.get("page_start"),
            page_end=topic.get("page_end"),
        )
    db.flush()
    points = [
        point
        for point in _points(db, source_id)
        if not is_meta_point(point.display_name or "")
        and not is_fragment_point(point.display_name or "")
    ]
    if not points:
        topic = (goal or "").strip().split("，")[0].strip()
        if topic and not is_generic_point(topic) and not is_meta_point(topic):
            point = ensure_knowledge_point(
                db,
                source_id,
                topic[:30],
                status="active",
                page_start=page_start,
                page_end=page_end,
            )
            db.flush()
            points = [point]
    return points


def _schedule_preview(version: LearningPlanVersion, options: dict | None) -> dict:
    try:
        return schedule_service.preview_schedule(version, options)
    except schedule_service.ScheduleError as exc:
        raise DiagnosisError(exc.code, exc.message) from exc


def _remember_schedule(session: DiagnosticSession, preview: dict) -> None:
    state = _state(session)
    state["schedule_options"] = {
        "start_date": preview.get("start_date"),
        "duration_days": preview.get("duration_days"),
        "target_date": preview.get("target_date"),
        "daily_minutes": preview.get("daily_minutes"),
    }
    _save_state(session, state)


def build_plan_preview(db: Session, session: DiagnosticSession, *, goal: str | None = None, question_count: int | None = None, learning_mode: str = "auto", force_new: bool = False, schedule_options: dict | None = None) -> dict:
    if session.status == "confirmed":
        raise DiagnosisError("session_confirmed", "诊断已确认")
    state = _state(session)
    if session.knowledge_base_id:
        specs = _session_source_specs(session)
        points = []
        for spec in specs:
            source_id = spec.get("source_id")
            source = db.get(SourceMaterial, source_id)
            if source is None or source.status != "active":
                continue
            points.extend(
                _prepare_source_points(
                    db,
                    source_id,
                    goal or session.target_goal,
                    page_start=spec.get("page_start"),
                    page_end=spec.get("page_end"),
                )
            )
        if not points:
            raise DiagnosisError(
                "plan_points_missing",
                "知识库范围内还没有可规划的考点，请补充资料或重新诊断。",
            )
        unlearned = set(state.get("unlearned_point_ids") or [])
        learned = set(state.get("learned_point_ids") or [])
        selected = [point for point in points if point.id not in unlearned] or points
        legacy_count = question_count in {3, 5, 8}
        mode = "auto" if legacy_count else (learning_mode if learning_mode in {"foundation", "focus"} else (state.get("learning_mode") or learning_mode or "auto"))
        if legacy_count:
            state["question_count"] = question_count
        _save_state(session, state)
        payload = PlanCreateRequest(
            title="知识库学习方案",
            sources=[
                PlanSourceItem(
                    source_id=spec.get("source_id"),
                    page_start=spec.get("page_start"),
                    page_end=spec.get("page_end"),
                )
                for spec in specs
                if spec.get("source_id")
            ],
            learned=[point.display_name for point in points if point.id in learned],
            unlearned=[point.display_name for point in points if point.id in unlearned],
            goal=(goal or session.target_goal or "补上本次薄弱点").strip(),
            points=[
                PlanPointItem(
                    knowledge_point_id=point.id,
                    display_name=point.display_name,
                    priority="review",
                    suggested_question_count=1,
                )
                for point in selected
            ],
            question_count=question_count if legacy_count else None,
            learning_mode=mode,
        )
        payload.unlearned_point_ids = sorted(unlearned)
        result = plan_service.suggest_kb_plan(
            db,
            specs,
            payload,
            knowledge_base_id=session.knowledge_base_id,
            session_id=session.id,
            force_new=force_new,
        )
        excluded_points = list(result.get("excluded_points") or [])
        seen_excluded = {
            normalize_point_name(item.get("display_name") or "")
            for item in excluded_points
        }
        for spec in specs:
            for item in _skipped_chapter_topics(
                db,
                spec.get("source_id"),
                spec.get("page_start"),
                spec.get("page_end"),
            ):
                normalized = normalize_point_name(item.get("display_name") or "")
                if normalized and normalized in seen_excluded:
                    continue
                seen_excluded.add(normalized)
                excluded_points.append(item)
        version = db.get(LearningPlanVersion, result["version"]["id"])
        _apply_diagnostic_basis(db, version, session)
        preview = _schedule_preview(version, schedule_options)
        _remember_schedule(session, preview)
        session.plan_version_id = version.id
        session.status = "preview"
        session.updated_at = now_iso()
        db.commit()
        return {
            "session": session_payload(db, session),
            "plan": plan_service.plan_payload(db, db.get(LearningPlan, version.plan_id)),
            "version": plan_service._version_payload(version),
            "modules": plan_service._version_payload(version)["modules"],
            "diagnostic_basis": [
                {
                    "knowledge_point_id": point_id,
                    "diagnostic_answer_id": answer_id,
                    "diagnostic_question_id": question_id,
                    "reason": "这次诊断题答错，建议本轮用新题验证。",
                }
                for point_id, answer_id, question_id in _wrong_points(db, session)
            ],
            "excluded_points": excluded_points,
            "unplaced_points": result.get("unplaced_points") or [],
            "missing": plan_service.confirmation_missing(db, version),
            "schedule_preview": preview,
        }
    source_spec = _session_source_specs(session)[0] if _session_source_specs(session) else {}
    points = _prepare_source_points(
        db,
        session.source_id,
        goal or session.target_goal,
        page_start=source_spec.get("page_start"),
        page_end=source_spec.get("page_end"),
    )
    if not points:
        raise DiagnosisError(
            "plan_points_missing",
            "诊断还没有定位可规划的考点，请补充一轮诊断后再生成方案。",
        )
    unlearned = set(state.get("unlearned_point_ids") or [])
    learned = set(state.get("learned_point_ids") or [])
    selected = [point for point in points if point.id not in unlearned]
    if not selected:
        selected = points
    legacy_count = question_count in {3, 5, 8}
    mode = "auto" if legacy_count else (learning_mode if learning_mode in {"foundation", "focus"} else (state.get("learning_mode") or learning_mode or "auto"))
    if legacy_count:
        state["question_count"] = question_count
    _save_state(session, state)
    payload = PlanCreateRequest(
        title="本次学习计划",
        sources=[PlanSourceItem(source_id=session.source_id)],
        learned=[point.display_name for point in points if point.id in learned],
        unlearned=[point.display_name for point in points if point.id in unlearned],
        goal=(goal or session.target_goal or "补上本次薄弱点").strip(),
        points=[
            PlanPointItem(
                knowledge_point_id=point.id,
                display_name=point.display_name,
                priority="review",
                suggested_question_count=1,
            )
            for point in selected
        ],
        question_count=question_count if legacy_count else None,
        learning_mode=mode,
    )
    payload.unlearned_point_ids = sorted(unlearned)
    result = plan_service.suggest_next_plan(db, session.source_id, payload)
    version = db.get(LearningPlanVersion, result["version"]["id"])
    version.diagnostic_session_id = session.id
    _apply_diagnostic_basis(db, version, session)
    preview = _schedule_preview(version, schedule_options)
    _remember_schedule(session, preview)
    session.plan_version_id = version.id
    session.status = "preview"
    session.updated_at = now_iso()
    db.commit()
    return {
        "session": session_payload(db, session),
        "plan": plan_service.plan_payload(db, db.get(LearningPlan, version.plan_id)),
        "version": plan_service._version_payload(version),
        "modules": plan_service._version_payload(version)["modules"],
        "diagnostic_basis": [
            {
                "knowledge_point_id": point_id,
                "diagnostic_answer_id": answer_id,
                "diagnostic_question_id": question_id,
                "reason": "这次诊断题答错，建议本轮用新题验证。",
            }
            for point_id, answer_id, question_id in _wrong_points(db, session)
        ],
        "missing": plan_service.confirmation_missing(db, version),
        "schedule_preview": preview,
    }


def _generation_key(session: DiagnosticSession, attempt_no: int = 0) -> str:
    base = f"diagnostic:{session.id}"
    return base if attempt_no <= 0 else f"{base}:retry:{attempt_no}"


def _attempt_from_key(key: str | None) -> int:
    if not key or ":retry:" not in key:
        return 0
    suffix = key.rsplit(":retry:", 1)[1]
    try:
        value = int(suffix)
    except (TypeError, ValueError):
        logger.warning("diagnostic invalid retry key key=%s", key)
        return 0
    return value if value >= 0 else 0


def _latest_job(db: Session, session: DiagnosticSession) -> GenerationJob | None:
    base = _generation_key(session, 0)
    return db.execute(
        select(GenerationJob)
        .where(
            or_(
                GenerationJob.idempotency_key == base,
                GenerationJob.idempotency_key.like(f"{base}:retry:%"),
            )
        )
        .order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc())
    ).scalars().first()


def _job_for_session(db: Session, session: DiagnosticSession) -> GenerationJob | None:
    return _latest_job(db, session)


def _generation_attempt_no(db: Session, session: DiagnosticSession) -> tuple[int, GenerationJob | None]:
    job = _latest_job(db, session)
    return (_attempt_from_key(job.idempotency_key), job) if job is not None else (0, None)


def _plan_view(db: Session, session: DiagnosticSession) -> dict:
    result = {"available": False, "plan_id": None, "version_id": None, "reason": None}
    version_id = session.plan_version_id
    if not version_id:
        result["reason"] = "plan_version_id_empty"
        return result
    version = db.get(LearningPlanVersion, version_id)
    if version is None:
        result["reason"] = "version_deleted_or_missing"
        return result
    if version.status != "confirmed":
        result["reason"] = "version_not_confirmed"
        return result
    plan = db.get(LearningPlan, version.plan_id)
    if plan is None:
        result["reason"] = "plan_deleted"
        return result
    exists = db.execute(
        select(LearningPlanVersion.id).where(
            LearningPlanVersion.id == version_id,
            LearningPlanVersion.plan_id == plan.id,
        )
    ).first()
    if exists is None:
        result["reason"] = "version_not_openable"
        return result
    source = db.get(SourceMaterial, session.source_id)
    if source is None or source.status != "active":
        result["reason"] = "source_unavailable"
        return result
    result.update(available=True, plan_id=plan.id, version_id=version_id)
    return result


def _generation_can_start(db: Session, session: DiagnosticSession) -> bool:
    if not session.plan_version_id:
        return False
    version = db.get(LearningPlanVersion, session.plan_version_id)
    if version is None:
        return False
    return db.get(LearningPlan, version.plan_id) is not None


def replan_required_for_job(db: Session, job: GenerationJob) -> bool:
    if job.error_code != "plan_allocation_mismatch":
        return False
    key = job.idempotency_key or ""
    session = None
    if key.startswith("diagnostic:"):
        parts = key.split(":")
        if len(parts) >= 2 and parts[1]:
            session = db.get(DiagnosticSession, parts[1])
    version_id = None
    try:
        request_payload = json.loads(job.request_json or "{}")
        version_id = request_payload.get("plan_version_id")
    except (TypeError, json.JSONDecodeError):
        version_id = None
    if not version_id and session is not None:
        version_id = session.plan_version_id
    version = db.get(LearningPlanVersion, version_id) if version_id else None
    if version is None:
        return False
    try:
        items = json.loads(version.items_json or "[]")
    except (TypeError, json.JSONDecodeError):
        items = []
    if any(is_meta_point(item.get("display_name") or "") for item in items):
        return True
    try:
        modules = json.loads(version.modules_json or "[]")
    except (TypeError, json.JSONDecodeError):
        modules = []
    return any(
        is_meta_point(name)
        for module in modules
        for name in (module.get("core_points") or [])
    )


def _job_result(
    db: Session, session: DiagnosticSession, job: GenerationJob | None, *, duplicate: bool
) -> dict:
    container = _container(db, session, None)
    plan_view = _plan_view(db, session)
    result = {
        "pack_id": None,
        "job_id": job.id if job is not None else None,
        "job_status": job.status if job is not None else None,
        "container_pack_id": container.id if container is not None else None,
        "duplicate": duplicate,
        "retry_available": False,
        "error": None,
        "plan_version_id": session.plan_version_id,
        "actions": [],
    }
    if job is None:
        if _generation_can_start(db, session):
            result["retry_available"] = True
            result["actions"] = ["retry_generation"]
        else:
            result["error"] = {
                "code": "plan_missing",
                "message": "已确认计划不可用",
                "retryable": False,
            }
        return result
    if job.status == "succeeded":
        pack = db.get(LearningPack, job.pack_id) if job.pack_id else None
        if pack is not None and pack.source_kind != "diagnostic":
            result["pack_id"] = pack.id
            result["actions"] = ["open_pack"]
        elif plan_view["available"]:
            result["actions"] = ["open_plan"]
            result["plan_id"] = plan_view["plan_id"]
            result["error"] = {
                "code": "pack_missing",
                "message": "学习计划已不可用，可从已确认计划重新发起生成",
                "retryable": False,
            }
        else:
            result["error"] = {
                "code": "plan_missing",
                "message": "学习计划已不可用且已确认计划不可用",
                "retryable": False,
            }
        return result
    if job.status == "queued":
        if job.launch_state in (None, "pending_dispatch"):
            result["retry_available"] = True
            result["actions"] = ["retry_generation"]
        else:
            result["actions"] = ["poll"]
        return result
    if job.status == "running":
        result["actions"] = ["poll"]
        return result
    if job.status in JOB_FAILED_STATUSES:
        if replan_required_for_job(db, job):
            result["retry_available"] = False
            result["actions"] = ["replan"]
            result["error"] = {
                "code": job.error_code,
                "message": "原计划无法区分部分考点，需重新制定学习计划",
                "retryable": False,
            }
            return result
        attempt_no = _attempt_from_key(job.idempotency_key)
        detail = (job.error_detail or "").strip()
        if attempt_no >= MAX_GENERATION_ATTEMPTS:
            result["retry_available"] = False
            result["error"] = {
                "code": "diagnostic_generation_retry_exhausted",
                "message": "生成任务已达到重试上限，无法继续生成"
                + (f"：{detail}" if detail else ""),
                "retryable": False,
            }
        else:
            result["retry_available"] = True
            result["actions"] = ["retry_generation"]
            if job.error_code:
                result["error"] = {
                    "code": job.error_code,
                    "message": job.error_detail or "",
                    "retryable": True,
                }
        return result
    return result


def _generation_view(db: Session, session: DiagnosticSession) -> dict | None:
    if session.status != "confirmed":
        return None
    job = _latest_job(db, session)
    result = _job_result(db, session, job, duplicate=False)
    keys = (
        "job_id",
        "job_status",
        "attempt_no",
        "retry_available",
        "container_pack_id",
        "pack_id",
        "plan_version_id",
        "error",
        "actions",
    )
    payload = {key: result.get(key) for key in keys}
    payload["attempt_no"] = _attempt_from_key(job.idempotency_key) if job is not None else 0
    if result.get("actions") == ["open_plan"] and result.get("plan_id"):
        payload["plan_id"] = result["plan_id"]
    return payload


def _launch_pending(job: GenerationJob | None) -> bool:
    return (
        job is not None
        and job.status == "queued"
        and job.launch_state in (None, "pending_dispatch")
    )


def _claim_dispatch(db: Session, job: GenerationJob) -> bool:
    claimed = db.execute(
        update(GenerationJob)
        .where(
            GenerationJob.id == job.id,
            GenerationJob.status == "queued",
            or_(
                GenerationJob.launch_state.is_(None),
                GenerationJob.launch_state == "pending_dispatch",
            ),
        )
        .values(launch_state="dispatching", updated_at=now_iso())
    )
    db.commit()
    return claimed.rowcount == 1


def _release_dispatch(db: Session, job: GenerationJob) -> bool:
    released = db.execute(
        update(GenerationJob)
        .where(
            GenerationJob.id == job.id,
            GenerationJob.status == "queued",
            GenerationJob.launch_state == "dispatching",
        )
        .values(
            launch_state="pending_dispatch",
            error_code="diagnostic_generation_start_failed",
            error_detail="生成任务已创建但未能启动，可重新调度",
            updated_at=now_iso(),
        )
    )
    db.commit()
    return released.rowcount == 1


def _start_failed_result(db: Session, session: DiagnosticSession, job: GenerationJob) -> dict:
    return {
        "pack_id": None,
        "job_id": job.id,
        "job_status": "queued",
        "container_pack_id": (_container(db, session, None).id if _container(db, session, None) else None),
        "duplicate": False,
        "retry_available": True,
        "error": {
            "code": "diagnostic_generation_start_failed",
            "message": "生成任务已创建但未能启动，可重新调度",
            "retryable": True,
        },
        "plan_version_id": session.plan_version_id,
    }


def _dispatch_existing(
    db: Session, session: DiagnosticSession, job: GenerationJob, *, duplicate: bool
) -> dict:
    from app.domain.generation import start_job

    if not _claim_dispatch(db, job):
        db.refresh(job)
        return _job_result(db, session, job, duplicate=True)
    try:
        start_job(job.id)
    except Exception:  # noqa: BLE001
        logger.warning("diagnostic start failed job=%s", job.id, exc_info=True)
        _release_dispatch(db, job)
        db.refresh(job)
        return _start_failed_result(db, session, job)
    return _job_result(db, session, job, duplicate=duplicate)


def _scope_text(db: Session, sources_payload: list[dict], fallback: SourceMaterial) -> str:
    parts: list[str] = []
    for spec in sources_payload:
        source = db.get(SourceMaterial, spec.get("source_id"))
        if source is None:
            continue
        if source.kind == "pdf" and spec.get("page_start"):
            parts.append(
                f"{source.display_name} 第 {spec['page_start']}–{spec['page_end']} 页"
            )
        else:
            parts.append(source.display_name)
    return "；".join(parts) or fallback.display_name


def _create_generation_job(
    db: Session, session: DiagnosticSession, source: SourceMaterial, *, attempt_no: int
) -> dict:
    version = db.get(LearningPlanVersion, session.plan_version_id) if session.plan_version_id else None
    plan = db.get(LearningPlan, version.plan_id) if version is not None else None
    if version is None or plan is None:
        return {
            "pack_id": None,
            "job_id": None,
            "job_status": None,
            "container_pack_id": (_container(db, session, None).id if _container(db, session, None) else None),
            "duplicate": False,
            "retry_available": False,
            "error": {"code": "plan_missing", "message": "已确认计划不可用", "retryable": False},
            "plan_version_id": session.plan_version_id,
            "actions": [],
        }
    if version.status == "draft":
        plan_service._mark_version_confirmed(plan, version, update_current=True)
    sources_payload = [
        {
            "source_id": spec.get("source_id"),
            "page_start": spec.get("page_start"),
            "page_end": spec.get("page_end"),
        }
        for spec in (_session_source_specs(session) if session.knowledge_base_id else [])
        if spec.get("source_id")
    ] or [{"source_id": source.id, "page_start": None, "page_end": None}]
    try:
        plan_constraints = plan_service.build_generation_constraints(
            db, version, sources_payload
        )
    except ValueError as exc:
        if str(exc) == "plan_point_ambiguous":
            container = _container(db, session, None)
            return {
                "pack_id": None,
                "job_id": None,
                "job_status": None,
                "container_pack_id": container.id if container is not None else None,
                "duplicate": False,
                "retry_available": False,
                "error": {
                    "code": "plan_point_ambiguous",
                    "message": "原计划无法区分部分考点，需重新制定学习计划",
                    "retryable": False,
                },
                "plan_version_id": session.plan_version_id,
                "actions": ["replan"],
            }
        raise
    payload = {
        "idempotency_key": _generation_key(session, attempt_no),
        "sources": sources_payload,
        "progress_text": session.target_goal or "按本次诊断计划学习",
        "difficulty": "consolidate",
        "question_count": version.question_count,
        "consent_ack": True,
        "parent_pack_id": None,
        "review_locus": [],
        "review_focus": [],
        "focus_points": [],
        "plan_version_id": version.id,
        "plan_constraints": plan_constraints,
        "scope_text": _scope_text(db, sources_payload, source),
    }
    from app.domain.generation import create_job

    job = None
    reused = False
    for _ in range(3):
        try:
            job, reused = create_job(db, payload)
            break
        except OperationalError:
            db.rollback()
            continue
        except IntegrityError as exc:
            db.rollback()
            if not _is_target_conflict(exc):
                logger.exception("diagnostic generation job integrity session=%s", session.id)
                break
            existing = _latest_job(db, session)
            if existing is not None and _attempt_from_key(existing.idempotency_key) == attempt_no:
                if _launch_pending(existing):
                    return _dispatch_existing(db, session, existing, duplicate=True)
                return _job_result(db, session, existing, duplicate=True)
            container = _container(db, session, None)
            if container is not None:
                result = _job_result(db, session, None, duplicate=True)
                result["container_pack_id"] = container.id
                return result
            if _has_gradable_questions(db, session):
                raise DiagnosisError(
                    "diagnostic_confirm_conflict",
                    "另一个确认请求正在写入诊断证据，请稍后重试",
                ) from exc
            break
        except Exception:
            db.rollback()
            logger.exception("diagnostic generation job create failed session=%s", session.id)
            job = None
            break
    if job is None:
        return {
            "pack_id": None,
            "job_id": None,
            "job_status": None,
            "container_pack_id": (_container(db, session, None).id if _container(db, session, None) else None),
            "duplicate": False,
            "retry_available": True,
            "error": {
                "code": "generation_create_failed",
                "message": "诊断已确认，但正式生成任务创建失败",
                "retryable": True,
            },
            "plan_version_id": session.plan_version_id,
            "actions": ["retry_generation"],
        }
    if reused:
        if _launch_pending(job):
            return _dispatch_existing(db, session, job, duplicate=True)
        return _job_result(db, session, job, duplicate=True)
    if job.status == "queued":
        return _dispatch_existing(db, session, job, duplicate=False)
    return _job_result(db, session, job, duplicate=False)


def _container(db: Session, session: DiagnosticSession, source: SourceMaterial | None) -> LearningPack | None:
    return db.execute(
        select(LearningPack).where(LearningPack.job_id == f"diagnostic:{session.id}")
    ).scalars().first()


def _write_diagnostic_evidence_once(
    db: Session,
    session: DiagnosticSession,
    source: SourceMaterial,
    *,
    keep_current_version: bool = True,
    preserve_confirmed_at: bool = False,
) -> LearningPack | None:
    container = _container(db, session, None)
    if container is not None:
        return container
    answers = db.execute(
        select(DiagnosticAnswer, DiagnosticQuestion)
        .join(DiagnosticQuestion, DiagnosticQuestion.id == DiagnosticAnswer.question_id)
        .where(
            DiagnosticAnswer.session_id == session.id,
            DiagnosticQuestion.status == "active",
        )
        .order_by(DiagnosticAnswer.created_at, DiagnosticAnswer.id)
    ).all()
    diagnostic_rows = [
        (a, q)
        for a, q in answers
        if q.kind == "diagnostic" and a.status == "answered" and a.is_correct is not None
    ]
    if not diagnostic_rows:
        return None

    version = db.get(LearningPlanVersion, session.plan_version_id) if session.plan_version_id else None
    if version is not None and version.status == "draft":
        plan = db.get(LearningPlan, version.plan_id)
        if plan is not None:
            plan_service._mark_version_confirmed(
                plan, version, update_current=keep_current_version
            )

    pack = LearningPack(
        id=new_id("pack"),
        display_title=f"诊断：{source.display_name}",
        source_kind="diagnostic",
        difficulty="diagnostic",
        question_count=len(diagnostic_rows),
        parent_pack_id=None,
        parent_label=None,
        job_id=f"diagnostic:{session.id}",
        handout_word_count=0,
        model_name="diagnostic",
        prompt_version=DIAGNOSIS_RULE_VERSION,
        created_at=now_iso(),
    )
    db.add(pack)
    db.flush()
    db.add(
        PackSource(
            pack_id=pack.id,
            source_id=source.id,
            source_name_snapshot=source.display_name,
            source_sha256_snapshot=source.sha256,
            source_kind_snapshot=source.kind,
        )
    )
    correct = sum(1 for answer, _ in diagnostic_rows if answer.is_correct is True)
    wrong = sum(1 for answer, _ in diagnostic_rows if answer.is_correct is False)
    attempt = Attempt(
        id=new_id("attempt"),
        pack_id=pack.id,
        status="submitted",
        idempotency_key=f"diagnostic:{session.id}",
        correct_count=correct,
        wrong_count=wrong,
        unanswered_count=0,
        total_count=len(diagnostic_rows),
        submitted_at=now_iso(),
        created_at=now_iso(),
    )
    db.add(attempt)
    db.flush()
    for index, (answer, diagnostic_question) in enumerate(diagnostic_rows, start=1):
        question = Question(
            id=new_id("question"),
            pack_id=pack.id,
            ordinal=index,
            stem=diagnostic_question.stem,
            options_json=diagnostic_question.options_json,
            answer_index=diagnostic_question.answer_index or 0,
            explanation=diagnostic_question.explanation or "",
            knowledge_point=diagnostic_question.knowledge_point_name or "诊断题",
            origin_question_id=None,
            variant_kind="diagnostic",
            difficulty_tag="diagnostic",
            is_image_based=False,
            stem_hash=stem_hash(diagnostic_question.stem),
            created_at=now_iso(),
        )
        db.add(question)
        db.flush()
        db.add(
            AttemptAnswer(
                attempt_id=attempt.id,
                question_id=question.id,
                selected_index=answer.selected_index,
                is_correct=answer.is_correct,
                answered_at=now_iso(),
            )
        )
        db.add(
            LearningEvidence(
                id=new_id("learning_evidence"),
                pack_id=pack.id,
                attempt_id=attempt.id,
                question_id=question.id,
                knowledge_point_id=diagnostic_question.knowledge_point_id,
                source_id=source.id,
                citation_id=None,
                diagnostic_answer_id=answer.id,
                result="correct" if answer.is_correct else "wrong",
                source_type="diagnostic",
                reliability="confirmed",
                submitted_at=attempt.submitted_at,
                created_at=now_iso(),
            )
        )
    for answer, diagnostic_question in answers:
        if diagnostic_question.kind != "scope":
            continue
        options = _json(diagnostic_question.options_json, [])
        text = ""
        if answer.selected_index is not None and 0 <= answer.selected_index < len(options):
            text = options[answer.selected_index].get("text", "")
        db.add(
            SelfAssessment(
                id=new_id("self_assessment"),
                knowledge_point_id=diagnostic_question.knowledge_point_id,
                text=text,
                source="diagnosis",
                diagnostic_session_id=session.id,
                kind="scope" if answer.selected_index in {0, 1} else "uncertain",
                created_at=now_iso(),
            )
        )
    session.status = "confirmed"
    if not preserve_confirmed_at and session.confirmed_at is None:
        session.confirmed_at = now_iso()
    session.updated_at = now_iso()
    db.flush()
    return pack


def _ensure_generation(
    db: Session, session: DiagnosticSession, source: SourceMaterial, *, duplicate: bool
) -> dict:
    version = db.get(LearningPlanVersion, session.plan_version_id) if session.plan_version_id else None
    if version is None or db.get(LearningPlan, version.plan_id) is None:
        return {
            "pack_id": None,
            "job_id": None,
            "job_status": None,
            "container_pack_id": (_container(db, session, None).id if _container(db, session, None) else None),
            "duplicate": duplicate,
            "retry_available": False,
            "error": {"code": "plan_missing", "message": "已确认计划不可用", "retryable": False},
            "plan_version_id": session.plan_version_id,
            "actions": [],
        }
    attempt_no, job = _generation_attempt_no(db, session)
    if job is not None:
        if job.status == "succeeded":
            return _job_result(db, session, job, duplicate=duplicate)
        if job.status == "queued":
            if _launch_pending(job):
                return _dispatch_existing(db, session, job, duplicate=True)
            return _job_result(db, session, job, duplicate=duplicate)
        if job.status == "running":
            return _job_result(db, session, job, duplicate=duplicate)
        if job.status in JOB_FAILED_STATUSES:
            if attempt_no >= MAX_GENERATION_ATTEMPTS:
                return _job_result(db, session, job, duplicate=duplicate)
            return _create_generation_job(db, session, source, attempt_no=attempt_no + 1)
        return _job_result(db, session, job, duplicate=duplicate)
    return _create_generation_job(db, session, source, attempt_no=0)


def _after_confirm_writes(db: Session, session: DiagnosticSession) -> None:
    return None


def _has_gradable_questions(db: Session, session: DiagnosticSession) -> bool:
    rows = db.execute(
        select(DiagnosticAnswer, DiagnosticQuestion)
        .join(DiagnosticQuestion, DiagnosticQuestion.id == DiagnosticAnswer.question_id)
        .where(
            DiagnosticAnswer.session_id == session.id,
            DiagnosticQuestion.status == "active",
        )
    ).all()
    return any(
        question.kind == "diagnostic"
        and answer.status == "answered"
        and answer.is_correct is not None
        for answer, question in rows
    )


def _is_target_conflict(exc: Exception) -> bool:
    text = str(exc).lower()
    if "attempt.idempotency_key" in text or "uq_attempt_idem" in text:
        return False
    return (
        "learning_pack.job_id" in text
        or "generation_job.idempotency_key" in text
        or "unique constraint failed: learning_pack.job_id" in text
        or "unique constraint failed: generation_job.idempotency_key" in text
    )


def _reload_confirm_state(db: Session, session: DiagnosticSession) -> dict:
    container = _container(db, session, None)
    job = _latest_job(db, session)
    formal = db.get(LearningPack, job.pack_id) if job is not None and job.pack_id else None
    return {
        "has_gradable": _has_gradable_questions(db, session),
        "container": container,
        "job": job,
        "formal_pack": formal,
        "attempts": db.execute(
            select(func.count()).select_from(Attempt).where(
                Attempt.idempotency_key == f"diagnostic:{session.id}"
            )
        ).scalar()
        or 0,
        "evidence": db.execute(
            select(func.count()).select_from(LearningEvidence).where(
                LearningEvidence.source_type == "diagnostic"
            )
        ).scalar()
        or 0,
    }


def _confirm_write_transaction(
    db: Session,
    session: DiagnosticSession,
    source: SourceMaterial,
    schedule_options: dict | None = None,
) -> tuple[LearningPack | None, bool]:
    claimed = db.execute(
        update(DiagnosticSession)
        .where(
            DiagnosticSession.id == session.id,
            DiagnosticSession.status.in_(("active", "preview")),
        )
        .values(status="confirmed", confirmed_at=now_iso(), updated_at=now_iso())
    )
    if claimed.rowcount != 1:
        db.rollback()
        return None, False
    session.status = "confirmed"
    if session.confirmed_at is None:
        session.confirmed_at = now_iso()
    session.updated_at = now_iso()
    container = _write_diagnostic_evidence_once(db, session, source)
    version = db.get(LearningPlanVersion, session.plan_version_id) if session.plan_version_id else None
    if version is not None:
        plan = db.get(LearningPlan, version.plan_id)
        if plan is not None and version.status == "draft":
            plan_service._mark_version_confirmed(plan, version, update_current=True)
        if plan is not None:
            try:
                schedule_service.ensure_schedule(db, plan, version, schedule_options)
            except schedule_service.ScheduleError as exc:
                raise DiagnosisError(exc.code, exc.message) from exc
    _after_confirm_writes(db, session)
    db.flush()
    return container, True


def _confirm_light_transaction(
    db: Session,
    session: DiagnosticSession,
    schedule_options: dict | None = None,
) -> bool:
    claimed = db.execute(
        update(DiagnosticSession)
        .where(
            DiagnosticSession.id == session.id,
            DiagnosticSession.status.in_(("active", "preview")),
        )
        .values(status="confirmed", confirmed_at=now_iso(), updated_at=now_iso())
    )
    if claimed.rowcount != 1:
        db.rollback()
        return False
    session.status = "confirmed"
    if session.confirmed_at is None:
        session.confirmed_at = now_iso()
    session.updated_at = now_iso()
    version = db.get(LearningPlanVersion, session.plan_version_id) if session.plan_version_id else None
    if version is not None:
        plan = db.get(LearningPlan, version.plan_id)
        if plan is not None and version.status == "draft":
            plan_service._mark_version_confirmed(plan, version, update_current=True)
        if plan is not None:
            try:
                schedule_service.ensure_schedule(db, plan, version, schedule_options)
            except schedule_service.ScheduleError as exc:
                raise DiagnosisError(exc.code, exc.message) from exc
    db.commit()
    return True


def _repair_confirmed_container(
    db: Session, session: DiagnosticSession, source: SourceMaterial
) -> LearningPack | None:
    if session.status != "confirmed" or session.confirmed_at is None:
        return None
    if not _has_gradable_questions(db, session):
        return None
    if _container(db, session, None) is not None:
        return None
    container = _write_diagnostic_evidence_once(
        db,
        session,
        source,
        keep_current_version=False,
        preserve_confirmed_at=True,
    )
    db.commit()
    return container


def _ensure_confirmed_schedule(
    db: Session, session: DiagnosticSession, schedule_options: dict | None
) -> None:
    version = db.get(LearningPlanVersion, session.plan_version_id) if session.plan_version_id else None
    if version is None or version.status != "confirmed":
        return
    plan = db.get(LearningPlan, version.plan_id)
    if plan is None:
        return
    try:
        _, created = schedule_service.ensure_schedule(db, plan, version, schedule_options)
    except schedule_service.ScheduleError as exc:
        raise DiagnosisError(exc.code, exc.message) from exc
    if created:
        db.commit()


def _confirm_result_from_state(
    db: Session,
    session: DiagnosticSession,
    source: SourceMaterial,
    schedule_options: dict | None = None,
) -> dict:
    state = _reload_confirm_state(db, session)
    if state["has_gradable"] and state["container"] is None:
        raise DiagnosisError(
            "diagnostic_confirm_conflict",
            "另一个确认请求正在写入诊断证据，请稍后重试",
        )
    _ensure_confirmed_schedule(db, session, schedule_options)
    result = _ensure_generation(db, session, source, duplicate=True)
    result["container_pack_id"] = (
        state["container"].id if state["container"] is not None else None
    )
    return result


def confirm_session(
    db: Session,
    session: DiagnosticSession,
    source: SourceMaterial,
    schedule_options: dict | None = None,
) -> dict:
    if not schedule_options or all(value is None for value in schedule_options.values()):
        schedule_options = _state(session).get("schedule_options")
    if session.status != "confirmed":
        try:
            container, claimed = _confirm_write_transaction(
                db, session, source, schedule_options
            )
            if claimed:
                db.commit()
        except IntegrityError as exc:
            db.rollback()
            if not _is_target_conflict(exc):
                logger.exception("confirm integrity error session=%s", session.id)
                raise DiagnosisError("internal_error", "确认失败，请稍后重试") from exc
            fresh = db.get(DiagnosticSession, session.id)
            if fresh is None:
                raise DiagnosisError("diagnostic_session_not_found", "诊断会话不存在")
            return _confirm_result_from_state(db, fresh, source, schedule_options)
        except OperationalError as exc:
            db.rollback()
            logger.warning("confirm busy session=%s code=busy_retry", session.id)
            if _has_gradable_questions(db, session):
                raise DiagnosisError(
                    "diagnostic_confirm_conflict",
                    "数据库繁忙，请稍后重试确认",
                ) from exc
            try:
                if not _confirm_light_transaction(db, session, schedule_options):
                    fresh = db.get(DiagnosticSession, session.id)
                    if fresh is None:
                        raise DiagnosisError(
                            "diagnostic_session_not_found", "诊断会话不存在"
                        )
                    return _confirm_result_from_state(db, fresh, source, schedule_options)
            except DiagnosisError:
                db.rollback()
                raise
            except OperationalError as retry_exc:
                db.rollback()
                raise DiagnosisError(
                    "diagnostic_confirm_conflict",
                    "数据库繁忙，请稍后重试确认",
                ) from retry_exc
            result = _ensure_generation(db, session, source, duplicate=False)
            result["container_pack_id"] = None
            return result
        except DiagnosisError:
            db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception("confirm transaction failed session=%s", session.id)
            raise DiagnosisError("internal_error", "确认失败，请稍后重试") from exc
        if not claimed:
            fresh = db.get(DiagnosticSession, session.id)
            if fresh is None:
                raise DiagnosisError("diagnostic_session_not_found", "诊断会话不存在")
            return _ensure_generation(db, fresh, source, duplicate=True)
        result = _ensure_generation(db, session, source, duplicate=False)
        result["container_pack_id"] = container.id if container is not None else result.get("container_pack_id")
        return result

    container = _container(db, session, None)
    if container is None and _has_gradable_questions(db, session):
        try:
            container = _repair_confirmed_container(db, session, source)
        except IntegrityError as exc:
            db.rollback()
            if not _is_target_conflict(exc):
                logger.exception("legacy repair integrity error session=%s", session.id)
                raise DiagnosisError("internal_error", "确认失败，请稍后重试") from exc
            fresh = db.get(DiagnosticSession, session.id)
            if fresh is None:
                raise DiagnosisError("diagnostic_session_not_found", "诊断会话不存在")
            return _confirm_result_from_state(db, fresh, source, schedule_options)
        except OperationalError as exc:
            db.rollback()
            logger.warning("legacy repair busy session=%s", session.id)
            raise DiagnosisError(
                "diagnostic_confirm_conflict", "数据库繁忙，请稍后重试确认"
            ) from exc
        except DiagnosisError:
            raise
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception("legacy repair failed session=%s", session.id)
            raise DiagnosisError("internal_error", "确认失败，请稍后重试") from exc
        if container is None:
            return _confirm_result_from_state(db, session, source, schedule_options)
    _ensure_confirmed_schedule(db, session, schedule_options)
    result = _ensure_generation(db, session, source, duplicate=True)
    result["container_pack_id"] = container.id if container is not None else result.get("container_pack_id")
    return result


def expand_session(db: Session, session: DiagnosticSession) -> dict:
    rounds = db.execute(
        select(DiagnosticRound)
        .where(DiagnosticRound.session_id == session.id)
        .order_by(DiagnosticRound.ordinal)
    ).scalars().all()
    payload_rounds = []
    for round_row in rounds:
        questions = db.execute(
            select(DiagnosticQuestion).where(DiagnosticQuestion.round_id == round_row.id)
        ).scalars().all()
        question_payloads = []
        for question in questions:
            answer = db.execute(
                select(DiagnosticAnswer).where(
                    DiagnosticAnswer.session_id == session.id,
                    DiagnosticAnswer.question_id == question.id,
                )
            ).scalars().first()
            payload = _question_payload(question, reveal=answer is not None)
            payload["answer"] = _documented_answer(db, answer) if answer is not None else None
            question_payloads.append(payload)
        payload_rounds.append(
            {
                "id": round_row.id,
                "ordinal": round_row.ordinal,
                "intent": round_row.intent,
                "questions": question_payloads,
            }
        )
    return {"session": session_payload(db, session), "rounds": payload_rounds}