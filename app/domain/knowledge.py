from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import KnowledgePoint, now_iso
from app.infra.ids import new_id

GENERIC_NAMES = {
    "网络基础",
    "基础知识",
    "综合能力",
    "计算机网络",
    "网络",
    "基础",
    "计算机基础",
}

_PREFIX_PATTERN = re.compile(r"^(再练|复习|巩固|练习)[:：]\s*")


def normalize_point_name(name: str) -> str:
    text = _PREFIX_PATTERN.sub("", (name or "").strip())
    text = re.sub(r"[\s\u3000]+", "", text)
    return text.casefold()


_FRAGMENT_POINT_RE = re.compile(r"^\d{1,2}/\d{1,2}$")
_FILE_POINT_RE = re.compile(r".*\.(?:pdf|txt|md|png|jpe?g|webp)$", re.IGNORECASE)


def is_fragment_point(name: str) -> bool:
    text = (name or "").strip()
    return bool(_FRAGMENT_POINT_RE.fullmatch(text) or _FILE_POINT_RE.fullmatch(text))


def is_generic_point(name: str) -> bool:
    normalized = normalize_point_name(name)
    return bool(normalized) and normalized in {normalize_point_name(item) for item in GENERIC_NAMES}


def find_knowledge_point(db: Session, source_id: str | None, display_name: str) -> KnowledgePoint | None:
    normalized = normalize_point_name(display_name)
    if not source_id or not normalized:
        return None
    return db.execute(
        select(KnowledgePoint).where(
            KnowledgePoint.source_id == source_id,
            KnowledgePoint.name_normalized == normalized,
        )
    ).scalars().first()


def ensure_knowledge_point(
    db: Session,
    source_id: str,
    display_name: str,
    status: str = "active",
    *,
    page_start: int | None = None,
    page_end: int | None = None,
) -> KnowledgePoint:
    existing = find_knowledge_point(db, source_id, display_name)
    if existing is not None:
        if page_start is not None and existing.page_start is None:
            existing.page_start = page_start
        if page_end is not None and existing.page_end is None:
            existing.page_end = page_end
        return existing
    point = KnowledgePoint(
        id=new_id("knowledge_point"),
        source_id=source_id,
        display_name=(display_name or "").strip(),
        name_normalized=normalize_point_name(display_name),
        status=status,
        page_start=page_start,
        page_end=page_end,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    db.add(point)
    db.flush()
    return point


def rename_knowledge_point(db: Session, point: KnowledgePoint, display_name: str) -> KnowledgePoint:
    normalized = normalize_point_name(display_name)
    if not normalized:
        raise ValueError("display_name_required")
    if is_generic_point(display_name):
        raise ValueError("knowledge_point_too_generic")
    duplicate = db.execute(
        select(KnowledgePoint).where(
            KnowledgePoint.source_id == point.source_id,
            KnowledgePoint.name_normalized == normalized,
            KnowledgePoint.id != point.id,
        )
    ).scalars().first()
    if duplicate is not None:
        raise ValueError("knowledge_point_duplicate")
    point.display_name = display_name.strip()
    point.name_normalized = normalized
    point.updated_at = now_iso()
    return point