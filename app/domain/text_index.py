from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import Evidence, SourceMaterial, now_iso
from app.infra import storage, text_engine
from app.infra.ids import new_id

logger = logging.getLogger("xunzhi.text_index")

TEXT_KINDS = {"text", "md"}
INDEX_VERSION = 1


def _locator(block: text_engine.TextBlock) -> dict:
    return {
        "chars": [block.start, block.end],
        "blocks": [block.ordinal],
        "rects": block.rects,
    }


def backfill_source(db: Session, source: SourceMaterial) -> dict:
    if source.kind not in TEXT_KINDS:
        return {"source_id": source.id, "skipped": True, "reason": "not_text"}
    try:
        path = storage.absolute(source.stored_path)
        if not path.exists():
            raise FileNotFoundError("original file missing")
        text = text_engine.decode_text(path.read_bytes())
        blocks = text_engine.split_text(text)
    except Exception as exc:  # noqa: BLE001
        source.text_index_error = f"{type(exc).__name__}: {exc}"[:300]
        source.text_index_version = 0
        db.commit()
        logger.warning("text index failed source=%s error=%s", source.id, source.text_index_error)
        return {"source_id": source.id, "ok": False, "error": source.text_index_error}

    rows = db.execute(
        select(Evidence)
        .where(
            Evidence.source_id == source.id,
            Evidence.kind == "text_span",
            Evidence.status == "active",
        )
        .order_by(Evidence.ordinal)
    ).scalars().all()
    for index, row in enumerate(rows):
        if index < len(blocks):
            block = blocks[index]
            row.quote_text = block.text[:200]
            row.body_text = block.text
            row.locator_json = json.dumps(_locator(block), ensure_ascii=False)
        elif not row.body_text:
            row.body_text = row.quote_text
    for block in blocks[len(rows):]:
        db.add(
            Evidence(
                id=new_id("evidence"),
                source_id=source.id,
                kind="text_span",
                page_no=None,
                ordinal=block.ordinal,
                locator_json=json.dumps(_locator(block), ensure_ascii=False),
                quote_text=block.text[:200],
                body_text=block.text,
                confidence="high",
                status="active",
            )
        )
    source.text_index_version = INDEX_VERSION
    source.text_index_error = None
    db.commit()
    logger.info(
        "text index rebuilt source=%s blocks=%s previous_evidence=%s",
        source.id,
        len(blocks),
        len(rows),
    )
    return {"source_id": source.id, "ok": True, "blocks": len(blocks), "evidence": max(len(rows), len(blocks))}


def backfill_all(db: Session, limit: int | None = None) -> list[dict]:
    query = (
        select(SourceMaterial)
        .where(
            SourceMaterial.status == "active",
            SourceMaterial.kind.in_(TEXT_KINDS),
            SourceMaterial.text_index_version < INDEX_VERSION,
        )
        .order_by(SourceMaterial.created_at, SourceMaterial.id)
    )
    if limit:
        query = query.limit(limit)
    sources = db.execute(query).scalars().all()
    results = []
    for source in sources:
        try:
            results.append(backfill_source(db, source))
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning("text index crashed source=%s error=%s", source.id, exc)
            results.append({"source_id": source.id, "ok": False, "error": str(exc)[:300]})
    return results