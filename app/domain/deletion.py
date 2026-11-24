from __future__ import annotations

import json
import math

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.domain.models import (
    Citation,
    Evidence,
    ImageAsset,
    LearningPack,
    PackSource,
    ReviewFocus,
    SourceMaterial,
    SourcePage,
    now_iso,
)
from app.infra import storage


class DeleteError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def affected_packs(db: Session, source_id: str) -> list[dict]:
    rows = db.execute(
        select(PackSource, LearningPack)
        .join(LearningPack, LearningPack.id == PackSource.pack_id)
        .where(PackSource.source_id == source_id, LearningPack.source_kind != "diagnostic")
    ).all()
    return [
        {
            "id": pack.id,
            "title": pack.display_title,
            "created_at": pack.created_at,
        }
        for _link, pack in rows
    ]


def delete_pack(db: Session, pack_id: str) -> dict:
    pack = db.get(LearningPack, pack_id)
    if pack is None:
        raise DeleteError("not_found", "学习计划不存在", 404)
    db.execute(delete(ReviewFocus).where(ReviewFocus.source_pack_id == pack_id))
    db.execute(delete(LearningPack).where(LearningPack.id == pack_id))
    db.commit()
    return {"deleted": True, "pack_id": pack_id}


def delete_source(db: Session, source_id: str) -> dict:
    source = db.get(SourceMaterial, source_id)
    if source is None:
        raise DeleteError("not_found", "资料不存在", 404)
    if source.status != "active":
        raise DeleteError("source_deleted", "资料已删除", 409)
    packs = affected_packs(db, source_id)
    asset_paths = [
        row.rel_path
        for row in db.execute(select(ImageAsset).where(ImageAsset.source_id == source_id)).scalars().all()
    ]

    db.execute(
        update(Evidence)
        .where(Evidence.source_id == source_id)
        .values(
            status="orphaned",
            quote_text=None,
            context_before=None,
            context_after=None,
            locator_json="{}",
        )
    )
    db.execute(
        update(SourcePage)
        .where(SourcePage.source_id == source_id)
        .values(page_image_asset_id=None)
    )
    db.execute(delete(ImageAsset).where(ImageAsset.source_id == source_id))
    source.status = "deleted"
    source.deleted_at = now_iso()
    source.cleanup_pending = 0
    db.commit()

    failed = False
    try:
        storage.remove_tree(storage.source_dir(source_id))
        storage.remove_tree(storage.derived_dir(source_id))
        for rel in asset_paths:
            path = storage.absolute(rel)
            if path.exists():
                path.unlink(missing_ok=True)
    except OSError:
        failed = True
    if storage.source_dir(source_id).exists() or storage.derived_dir(source_id).exists():
        failed = True
    if failed:
        source.cleanup_pending = 1
        db.commit()
    return {
        "deleted": True,
        "source_id": source_id,
        "affected_packs": packs,
        "cleanup_pending": failed,
    }


def cleanup_pending_sources(db: Session) -> int:
    rows = db.execute(
        select(SourceMaterial).where(SourceMaterial.cleanup_pending == 1, SourceMaterial.status == "deleted")
    ).scalars().all()
    cleaned = 0
    for source in rows:
        try:
            storage.remove_tree(storage.source_dir(source.id))
            storage.remove_tree(storage.derived_dir(source.id))
            source.cleanup_pending = 0
            cleaned += 1
        except OSError:
            continue
    if cleaned:
        db.commit()
    return cleaned


def source_status_map(db: Session, pack_id: str) -> dict[str, str]:
    rows = db.execute(select(PackSource).where(PackSource.pack_id == pack_id)).scalars().all()
    result: dict[str, str] = {}
    for link in rows:
        source = db.get(SourceMaterial, link.source_id)
        result[link.source_id] = source.status if source else "deleted"
    return result


def citation_in_pack(db: Session, citation: Citation) -> bool:
    pack = db.get(LearningPack, citation.pack_id)
    if pack is None:
        return False
    link = db.get(PackSource, (citation.pack_id, citation.source_id))
    if link is None:
        return False
    evidence = db.get(Evidence, citation.evidence_id)
    if evidence is None:
        return False
    if evidence.source_id != citation.source_id:
        return False
    if citation.page_no is not None and link.page_start is not None:
        if not (link.page_start <= citation.page_no <= (link.page_end or link.page_start)):
            return False
    return True


def locator_valid(citation: Citation) -> bool:
    try:
        locator = json.loads(citation.locator_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(locator, dict):
        return False
    boxes = locator.get("boxes")
    if not boxes:
        box_2d = locator.get("box_2d")
        if box_2d:
            boxes = [box_2d]
    if not boxes:
        rects = locator.get("rects")
        if rects:
            boxes = rects
    if not boxes and locator.get("rect"):
        boxes = [locator["rect"]]
    boxes = boxes or []
    for box in boxes:
        if not isinstance(box, list) or len(box) != 4:
            return False
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or value > 1
            for value in box
        ):
            return False
        if box[2] <= box[0] or box[3] <= box[1]:
            return False
    return True