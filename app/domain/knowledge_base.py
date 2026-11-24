from __future__ import annotations

import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.models import (
    Evidence,
    KnowledgeBase,
    KnowledgeBaseItem,
    SourceMaterial,
    SourcePage,
    now_iso,
)
from app.infra import pdf_engine, source_service, storage
from app.infra.db import SessionLocal
from app.infra.ids import new_id

STATUS_LABELS = {
    "pending": "待整理",
    "processing": "整理中",
    "ready": "可学习",
    "failed": "失败",
}


class KnowledgeBaseError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _get_base(db: Session, kb_id: str) -> KnowledgeBase:
    base = db.get(KnowledgeBase, kb_id)
    if base is None:
        raise KnowledgeBaseError("knowledge_base_not_found", "知识库不存在", 404)
    return base


def _get_item(db: Session, kb_id: str, item_id: str) -> KnowledgeBaseItem:
    item = db.get(KnowledgeBaseItem, item_id)
    if item is None or item.knowledge_base_id != kb_id:
        raise KnowledgeBaseError("knowledge_base_item_not_found", "知识库资料不存在", 404)
    return item


def _page_count(db: Session, source_id: str | None) -> int:
    if not source_id:
        return 0
    return int(
        db.execute(
            select(SourcePage.page_no).where(SourcePage.source_id == source_id)
        ).all().__len__()
    )


MAX_CHAPTERS = 20
MAX_HEADING_CHAPTERS = 12
FALLBACK_CHUNK = 25
_HEADING_STOP_CHARS = "。，；：、,.;:!?"


def _chapter_label(title: str, start: int, end: int) -> str:
    span = f"第 {start} 页" if start == end else f"第 {start}–{end} 页"
    return f"{title} · {span}" if title else span


def _heading_of(quote: str | None) -> str:
    line = (quote or "").strip().splitlines()[0].strip() if (quote or "").strip() else ""
    if not (2 <= len(line) <= 24):
        return ""
    if line.isdigit() or any(ch in line for ch in _HEADING_STOP_CHARS):
        return ""
    return line


def _toc_chapters(doc, page_count: int) -> list[dict]:
    rows = pdf_engine.read_outline(doc)
    if not rows:
        return []
    level = min(item[0] for item in rows)
    picks = [item for item in rows if item[0] == level and item[2] <= page_count]
    if not picks or len(picks) > MAX_CHAPTERS:
        return []
    chapters: list[dict] = []
    for index, (_, title, start) in enumerate(picks):
        end = picks[index + 1][2] - 1 if index + 1 < len(picks) else page_count
        if end < start:
            continue
        chapters.append(
            {
                "title": title,
                "page_start": start,
                "page_end": end,
                "label": _chapter_label(title, start, end),
                "origin": "toc",
            }
        )
    return chapters


def _heading_chapters(db: Session, source_id: str, page_count: int) -> list[dict]:
    rows = db.execute(
        select(Evidence.page_no, Evidence.quote_text)
        .where(
            Evidence.source_id == source_id,
            Evidence.kind == "text_span",
            Evidence.status == "active",
        )
        .order_by(Evidence.page_no, Evidence.ordinal)
    ).all()
    first: dict[int, str] = {}
    for page_no, quote in rows:
        if not page_no or page_no in first:
            continue
        heading = _heading_of(quote)
        if heading:
            first[page_no] = heading
    if len(first) < 2:
        return []
    chapters: list[dict] = []
    for page_no in range(1, page_count + 1):
        heading = first.get(page_no)
        if not heading:
            if chapters:
                chapters[-1]["page_end"] = page_no
            continue
        if chapters and chapters[-1]["title"] == heading:
            chapters[-1]["page_end"] = page_no
        else:
            chapters.append(
                {"title": heading, "page_start": page_no, "page_end": page_no}
            )
    if not chapters or len(chapters) > MAX_HEADING_CHAPTERS:
        return []
    chapters[0]["page_start"] = 1
    chapters[-1]["page_end"] = page_count
    for chapter in chapters:
        chapter["label"] = _chapter_label(
            chapter["title"], chapter["page_start"], chapter["page_end"]
        )
        chapter["origin"] = "heading"
    return chapters


def _fixed_chapters(page_count: int) -> list[dict]:
    if page_count <= 0:
        return []
    step = max(FALLBACK_CHUNK, (page_count + 7) // 8)
    chapters: list[dict] = []
    start = 1
    while start <= page_count:
        end = min(page_count, start + step - 1)
        chapters.append(
            {
                "title": "",
                "page_start": start,
                "page_end": end,
                "label": _chapter_label("", start, end),
                "origin": "fixed",
            }
        )
        start = end + 1
    return chapters


def chapters_for_source(db: Session, source: SourceMaterial | None, page_count: int) -> list[dict]:
    if source is None or source.kind != "pdf" or page_count <= 0:
        return []
    try:
        path = storage.absolute(source.stored_path)
        if not path.exists():
            return []
        doc = pdf_engine.open_document(str(path))
    except Exception:  # noqa: BLE001
        return []
    try:
        chapters = _toc_chapters(doc, page_count)
    finally:
        doc.close()
    if chapters:
        return chapters
    chapters = _heading_chapters(db, source.id, page_count)
    return chapters or _fixed_chapters(page_count)


def serialize_item(db: Session, item: KnowledgeBaseItem) -> dict:
    source = db.get(SourceMaterial, item.source_id) if item.source_id else None
    available = source is not None and source.status == "active"
    pages = _page_count(db, item.source_id) if available else 0
    display = item.display_name or (source.display_name if source is not None else "资料已删除")
    return {
        "id": item.id,
        "source_id": item.source_id,
        "display_name": display,
        "original_name": source.original_name if source is not None else "",
        "kind": source.kind if source is not None else "",
        "ext": source.ext if source is not None else "",
        "size_bytes": source.size_bytes if source is not None else 0,
        "page_count": pages or (source.page_count or 0) if source is not None else 0,
        "status": "failed" if item.status == "failed" else ("ready" if available else item.status),
        "status_label": STATUS_LABELS.get(item.status, item.status),
        "usable": available,
        "error_code": item.error_code,
        "error_message": item.error_message,
        "note": item.note,
        "version_no": item.version_no,
        "source_available": available,
        "text_index_status": (
            "ready"
            if source is not None and source.text_index_version >= 1
            else ("failed" if source is not None and source.text_index_error else "pending")
        )
        if source is not None and source.kind in {"text", "md"}
        else None,
        "text_index_error": source.text_index_error if source is not None else None,
        "chapters": chapters_for_source(db, source, pages) if available else [],
        "updated_at": item.updated_at,
    }


def serialize_base(db: Session, base: KnowledgeBase, *, include_items: bool = True) -> dict:
    items = db.execute(
        select(KnowledgeBaseItem)
        .where(KnowledgeBaseItem.knowledge_base_id == base.id)
        .order_by(KnowledgeBaseItem.position, KnowledgeBaseItem.created_at)
    ).scalars().all()
    item_payloads = [serialize_item(db, item) for item in items]
    usable = [item for item in item_payloads if item["usable"]]
    failed = [item for item in item_payloads if item["status"] == "failed"]
    pending = [item for item in item_payloads if item["status"] in {"pending", "processing"}]
    status = "ready"
    if pending:
        status = "processing"
    elif failed:
        status = "failed"
    elif not usable:
        status = "empty"
    payload = {
        "id": base.id,
        "name": base.name,
        "description": base.description,
        "status": status,
        "status_label": {"ready": "已整理", "processing": "整理中", "failed": "有失败项", "empty": "空"}[status],
        "item_count": len(items),
        "ready_count": len(usable),
        "failed_count": len(failed),
        "total_pages": sum(int(item["page_count"] or 0) for item in usable),
        "created_at": base.created_at,
        "updated_at": base.updated_at,
    }
    if include_items:
        payload["items"] = item_payloads
    return payload


def list_bases(db: Session) -> list[dict]:
    rows = db.execute(select(KnowledgeBase).order_by(KnowledgeBase.created_at)).scalars().all()
    return [serialize_base(db, row, include_items=False) for row in rows]


def create_base(db: Session, name: str, description: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise KnowledgeBaseError("name_required", "知识库名称不能为空", 422)
    base = KnowledgeBase(
        id=new_id("knowledge_base"),
        name=name[:80],
        description=(description or "").strip(),
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    db.add(base)
    db.commit()
    return serialize_base(db, base)


def update_base(db: Session, kb_id: str, *, name: str | None = None, description: str | None = None) -> dict:
    base = _get_base(db, kb_id)
    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise KnowledgeBaseError("name_required", "知识库名称不能为空", 422)
        base.name = cleaned[:80]
    if description is not None:
        base.description = description.strip()
    base.updated_at = now_iso()
    db.commit()
    return serialize_base(db, base)


def delete_base(db: Session, kb_id: str) -> dict:
    base = _get_base(db, kb_id)
    items = db.execute(
        select(KnowledgeBaseItem).where(KnowledgeBaseItem.knowledge_base_id == kb_id)
    ).scalars().all()
    for item in items:
        db.delete(item)
    db.delete(base)
    db.commit()
    return {"deleted": True, "knowledge_base_id": kb_id, "unlinked_items": len(items)}


def _next_position(db: Session, kb_id: str) -> int:
    rows = db.execute(
        select(KnowledgeBaseItem.position).where(KnowledgeBaseItem.knowledge_base_id == kb_id)
    ).scalars().all()
    values = [value for value in rows if isinstance(value, int)]
    return (max(values) + 1) if values else len(rows)


def add_existing_source(db: Session, kb_id: str, source_id: str, note: str = "") -> dict:
    base = _get_base(db, kb_id)
    source = db.get(SourceMaterial, source_id)
    if source is None or source.status != "active":
        raise KnowledgeBaseError("source_not_found", "资料不存在或已删除", 404)
    existing = db.execute(
        select(KnowledgeBaseItem).where(
            KnowledgeBaseItem.knowledge_base_id == kb_id,
            KnowledgeBaseItem.source_id == source_id,
        )
    ).scalars().first()
    if existing is not None:
        return serialize_base(db, base)
    item = KnowledgeBaseItem(
        id=new_id("kb_item"),
        knowledge_base_id=kb_id,
        source_id=source_id,
        display_name=source.display_name,
        note=(note or "").strip(),
        status="ready",
        version_no=1,
        position=_next_position(db, kb_id),
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    db.add(item)
    base.updated_at = now_iso()
    db.commit()
    return serialize_base(db, base)


def update_item(
    db: Session,
    kb_id: str,
    item_id: str,
    *,
    display_name: str | None = None,
    note: str | None = None,
) -> dict:
    item = _get_item(db, kb_id, item_id)
    if display_name is not None:
        item.display_name = display_name.strip()[:120] or (item.display_name or "资料")

    if note is not None:
        item.note = note.strip()
    item.updated_at = now_iso()
    db.commit()
    return serialize_base(db, _get_base(db, kb_id))


def remove_item(db: Session, kb_id: str, item_id: str) -> dict:
    item = _get_item(db, kb_id, item_id)
    db.delete(item)
    db.commit()
    return {"removed": True, "knowledge_base_id": kb_id, "item_id": item_id, "original_file_kept": True}


def _ingest_item(item_id: str, filename: str, data: bytes, replace: bool) -> None:
    with SessionLocal() as db:
        item = db.get(KnowledgeBaseItem, item_id)
        if item is None:
            return
        item.status = "processing"
        item.error_code = None
        item.error_message = None
        item.updated_at = now_iso()
        db.commit()
        try:
            source, _reused = source_service.import_source(db, filename, data, lazy_pages=True)
            db.commit()
            item = db.get(KnowledgeBaseItem, item_id)
            if item is None:
                return
            item.source_id = source.id
            item.status = "ready"
            item.error_code = None
            item.error_message = None
            if replace:
                item.version_no = (item.version_no or 1) + 1
            if not item.display_name:
                item.display_name = source.display_name
            item.updated_at = now_iso()
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            item = db.get(KnowledgeBaseItem, item_id)
            if item is not None:
                item.status = "failed"
                item.error_code = getattr(exc, "code", "ingest_failed")
                item.error_message = getattr(exc, "message", str(exc))[:500]
                item.updated_at = now_iso()
                db.commit()


def add_upload(db: Session, kb_id: str, filename: str, data: bytes) -> dict:
    base = _get_base(db, kb_id)
    if data and filename.lower().endswith(".pdf"):
        try:
            doc = source_service.pdf_engine.open_document_bytes(data)
            pages = source_service.pdf_engine.page_count(doc)
            doc.close()
        except Exception as exc:  # noqa: BLE001
            raise KnowledgeBaseError("file_unreadable", f"PDF 无法读取：{exc}", 422) from exc
        existing_pages = sum(
            serialize_item(db, item)["page_count"]
            for item in db.execute(
                select(KnowledgeBaseItem).where(KnowledgeBaseItem.knowledge_base_id == kb_id)
            ).scalars().all()
        )
        if existing_pages + pages > settings.kb_max_pages:
            raise KnowledgeBaseError(
                "kb_page_limit",
                f"知识库最多 {settings.kb_max_pages} 页，当前已用 {existing_pages} 页",
                413,
            )
    item = KnowledgeBaseItem(
        id=new_id("kb_item"),
        knowledge_base_id=kb_id,
        source_id=None,
        display_name=filename,
        note="",
        status="pending",
        version_no=1,
        position=_next_position(db, kb_id),
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    db.add(item)
    base.updated_at = now_iso()
    db.commit()
    thread = threading.Thread(
        target=_ingest_item,
        args=(item.id, filename, data, False),
        name=f"kb-ingest-{item.id}",
        daemon=True,
    )
    thread.start()
    return serialize_base(db, base)


def replace_item_upload(db: Session, kb_id: str, item_id: str, filename: str, data: bytes) -> dict:
    _get_item(db, kb_id, item_id)
    item = db.get(KnowledgeBaseItem, item_id)
    item.status = "processing"
    item.error_code = None
    item.error_message = None
    item.updated_at = now_iso()
    db.commit()
    thread = threading.Thread(
        target=_ingest_item,
        args=(item_id, filename, data, True),
        name=f"kb-replace-{item_id}",
        daemon=True,
    )
    thread.start()
    return serialize_base(db, _get_base(db, kb_id))


def retry_item(db: Session, kb_id: str, item_id: str) -> dict:
    item = _get_item(db, kb_id, item_id)
    if item.status != "failed":
        return serialize_base(db, _get_base(db, kb_id))
    # A failed upload has no stored bytes; require the user to replace/upload again.
    raise KnowledgeBaseError(
        "retry_needs_file",
        "整理失败的文件需要重新上传或替换后才能重试",
        409,
    )


def scope_specs(
    db: Session, kb_id: str, mode: str = "all", source_ids: list[str] | None = None, page_ranges: dict | None = None
) -> list[dict]:
    base = _get_base(db, kb_id)
    items = db.execute(
        select(KnowledgeBaseItem).where(KnowledgeBaseItem.knowledge_base_id == base.id)
    ).scalars().all()
    specs = []
    selected = set(source_ids or [])
    ranges = page_ranges or {}
    for item in items:
        if not item.source_id:
            continue
        source = db.get(SourceMaterial, item.source_id)
        if source is None or source.status != "active":
            continue
        if mode != "all" and selected and item.source_id not in selected:
            continue
        page_range = {} if mode == "all" else (ranges.get(item.source_id) or {})
        specs.append(
            {
                "source_id": item.source_id,
                "source_name": item.display_name or source.display_name,
                "page_start": page_range.get("page_start"),
                "page_end": page_range.get("page_end"),
                "page_count": _page_count(db, item.source_id) or source.page_count,
                "note": item.note,
            }
        )
    if not specs:
        raise KnowledgeBaseError("knowledge_base_scope_empty", "所选范围内没有已整理、可学习的资料", 409)
    if len(specs) > settings.kb_max_sources:
        raise KnowledgeBaseError("kb_source_limit", f"知识库单次学习最多 {settings.kb_max_sources} 份资料", 413)
    return specs