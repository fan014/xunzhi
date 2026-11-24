from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.models import (
    AppSetting,
    Evidence,
    ImageAsset,
    SourceMaterial,
    SourcePage,
    now_iso,
)
from app.infra import image_engine, pdf_engine, storage, text_engine
from app.infra.ids import new_id

logger = logging.getLogger("xunzhi.source")

EXT_WHITELIST = {"pdf", "txt", "md", "png", "jpg", "jpeg", "webp"}
MIME_BY_EXT = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}

REGION_INDEX_PREFIX = "pdf.region_index."
REGION_INDEX_VERSION = 1
_SOURCE_VISUAL_LOCK = threading.RLock()


class ImportError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _ext_of(filename: str) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext == "jpeg":
        return "jpeg"
    return ext


def _validate(ext: str, data: bytes) -> dict:
    if ext not in EXT_WHITELIST:
        raise ImportError("unsupported_format", "暂不支持该格式，可另存为 PDF 或截图后上传")
    if not data:
        raise ImportError("file_unreadable", "文件为空，无法读取")
    if ext == "pdf":
        if not data.startswith(b"%PDF"):
            raise ImportError("file_unreadable", "文件内容不是有效的 PDF")
        try:
            doc = pdf_engine.open_document_bytes(data)
            count = pdf_engine.page_count(doc)
            doc.close()
        except Exception as exc:  # noqa: BLE001
            raise ImportError("file_unreadable", f"PDF 无法读取：{exc}") from exc
        return {"kind": "pdf", "page_count": count, "mime": "application/pdf"}
    if ext in {"png", "jpg", "jpeg", "webp"}:
        try:
            info = image_engine.inspect_image(data)
        except ValueError as exc:
            raise ImportError("file_unreadable", str(exc)) from exc
        return {"kind": "image", "page_count": None, "mime": info["mime"], "width": info["width"], "height": info["height"]}
    try:
        text = text_engine.decode_text(data)
    except ValueError as exc:
        raise ImportError("file_unreadable", "文本文件无法按 UTF-8 或 GBK 解码") from exc
    if not text.strip():
        raise ImportError("file_unreadable", "文本文件为空")
    return {"kind": "text", "page_count": None, "mime": MIME_BY_EXT.get(ext, "text/plain"), "text": text}


def find_reusable(db: Session, sha: str, size: int) -> SourceMaterial | None:
    stmt = select(SourceMaterial).where(
        SourceMaterial.sha256 == sha,
        SourceMaterial.size_bytes == size,
        SourceMaterial.status == "active",
    )
    return db.execute(stmt).scalars().first()


def import_source(db: Session, filename: str, data: bytes, *, lazy_pages: bool = False) -> tuple[SourceMaterial, bool]:
    ext = _ext_of(filename)
    info = _validate(ext, data)
    sha = image_engine.sha256(data)
    existing = find_reusable(db, sha, len(data))
    if existing is not None:
        return existing, True

    source_id = new_id("source")
    directory = storage.source_dir(source_id)
    path = storage.original_path(source_id, ext)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        storage.atomic_write(path, data)
        material = SourceMaterial(
            id=source_id,
            display_name=filename,
            original_name=filename,
            kind=info["kind"],
            ext=ext,
            mime=info["mime"],
            size_bytes=len(data),
            sha256=sha,
            page_count=info.get("page_count"),
            status="active",
            stored_path=storage.relative(path),
            created_at=now_iso(),
        )
        db.add(material)
        db.flush()

        if info["kind"] == "image":
            _register_image(db, material, data, path)
        elif info["kind"] == "text":
            _register_text(db, material, info["text"])
        else:
            _register_pdf(db, material, path, lazy_pages=lazy_pages)
        db.flush()
        return material, False
    except Exception:
        db.rollback()
        storage.remove_tree(directory)
        storage.remove_tree(storage.derived_dir(source_id))
        raise


def _register_evidence(
    db: Session,
    source_id: str,
    kind: str,
    page_no: int | None,
    ordinal: int,
    locator: dict,
    quote: str | None = None,
    body: str | None = None,
    before: str | None = None,
    after: str | None = None,
) -> Evidence:
    evidence = Evidence(
        id=new_id("evidence"),
        source_id=source_id,
        kind=kind,
        page_no=page_no,
        ordinal=ordinal,
        locator_json=json.dumps(locator, ensure_ascii=False),
        quote_text=quote,
        body_text=body,
        context_before=before,
        context_after=after,
        confidence="high",
        status="active",
    )
    db.add(evidence)
    return evidence


def _register_image(db: Session, material: SourceMaterial, data: bytes, original: Path) -> None:
    info = image_engine.inspect_image(data)
    asset = ImageAsset(
        id=new_id("asset"),
        source_id=material.id,
        kind="original",
        page_no=None,
        rel_path=storage.relative(original),
        mime=info["mime"],
        width=info["width"],
        height=info["height"],
        size_bytes=len(data),
        sha256=image_engine.sha256(data),
    )
    db.add(asset)
    thumb = image_engine.make_thumb(data)
    thumb_path = storage.thumbs_dir(material.id) / f"{asset.id}.jpg"
    storage.atomic_write(thumb_path, thumb)
    db.add(
        ImageAsset(
            id=new_id("asset"),
            source_id=material.id,
            kind="thumb",
            page_no=None,
            rel_path=storage.relative(thumb_path),
            mime="image/jpeg",
            width=min(480, info["width"]),
            height=min(480, info["height"]),
            size_bytes=len(thumb),
            sha256=image_engine.sha256(thumb),
        )
    )
    _register_evidence(
        db,
        material.id,
        "image_region",
        None,
        1,
        {"asset_id": asset.id, "boxes": [[0.0, 0.0, 1.0, 1.0]]},
        quote=f"图片：{material.display_name}（{info['width']}×{info['height']}）",
        before=None,
        after=None,
    )


def _register_text(db: Session, material: SourceMaterial, text: str) -> None:
    blocks = text_engine.split_text(text)
    for block in blocks:
        _register_evidence(
            db,
            material.id,
            "text_span",
            None,
            block.ordinal,
            {"chars": [block.start, block.end], "blocks": [block.ordinal], "rects": block.rects},
            quote=block.text[:200],
            body=block.text,
            before=None,
            after=None,
        )


def _register_pdf(db: Session, material: SourceMaterial, path: Path, *, lazy_pages: bool = False) -> None:
    doc = pdf_engine.open_document(str(path))
    ordinal = 0
    try:
        for index in range(doc.page_count):
            page = doc[index]
            page_no = index + 1
            parsed = pdf_engine.extract_page(page, page_no)
            boxes = pdf_engine.page_image_boxes(page)
            asset_id = None
            if parsed.is_scanned or not lazy_pages:
                page_png = pdf_engine.render_page(page)
                asset_id = new_id("asset")
                page_path = storage.pages_dir(material.id) / f"p{page_no:04d}.png"
                storage.atomic_write(page_path, page_png)
                db.add(
                    ImageAsset(
                        id=asset_id,
                        source_id=material.id,
                        kind="pdf_page_render",
                        page_no=page_no,
                        rel_path=storage.relative(page_path),
                        mime="image/png",
                        width=0,
                        height=0,
                        size_bytes=len(page_png),
                        sha256=image_engine.sha256(page_png),
                    )
                )
            db.add(
                SourcePage(
                    source_id=material.id,
                    page_no=page_no,
                    char_count=parsed.char_count,
                    has_text=parsed.has_text,
                    is_scanned=parsed.is_scanned,
                    text_block_count=len(parsed.blocks),
                    page_image_asset_id=asset_id,
                )
            )
            for block in parsed.blocks:
                ordinal += 1
                _register_evidence(
                    db,
                    material.id,
                    "text_span",
                    page_no,
                    ordinal,
                    {"chars": [0, len(block.text)], "blocks": [ordinal], "rects": [block.rect]},
                    quote=block.text[:200],
                    body=block.text,
                )
            if parsed.is_scanned and asset_id:
                ordinal += 1
                _register_evidence(
                    db,
                    material.id,
                    "page_image",
                    page_no,
                    ordinal,
                    {"asset_id": asset_id, "rect": [0.0, 0.0, 1.0, 1.0], "scanned": True},
                    quote=f"{material.display_name} 第 {page_no} 页（扫描页，按图像定位）",
                )
            elif not lazy_pages and asset_id:
                ordinal += 1
                _register_evidence(
                    db,
                    material.id,
                    "page_image",
                    page_no,
                    ordinal,
                    {"asset_id": asset_id, "rect": [0.0, 0.0, 1.0, 1.0], "scanned": False},
                    quote=f"{material.display_name} 第 {page_no} 页",
                )
            for box in boxes:
                ordinal += 1
                locator = {"boxes": [box]}
                if asset_id:
                    locator["asset_id"] = asset_id
                else:
                    locator["deferred"] = True
                _register_evidence(
                    db,
                    material.id,
                    "image_region",
                    page_no,
                    ordinal,
                    locator,
                    quote=f"{material.display_name} 第 {page_no} 页内嵌图片区域",
                )
        _write_region_index(db, material.id, complete=True, pages=doc.page_count)
    finally:
        doc.close()


def _region_box_key(box) -> tuple[int, ...]:
    return tuple(int(round(float(value) * 1_000_000)) for value in box)


def _region_index_key(source_id: str) -> str:
    return f"{REGION_INDEX_PREFIX}{source_id}"


def _region_index_complete(db: Session, source_id: str) -> bool:
    row = db.get(AppSetting, _region_index_key(source_id))
    if row is None or not row.value:
        return False
    try:
        data = json.loads(row.value)
    except (TypeError, json.JSONDecodeError):
        return False
    return bool(data.get("complete")) and int(data.get("version") or 0) == REGION_INDEX_VERSION


def _write_region_index(db: Session, source_id: str, *, complete: bool, pages: int) -> None:
    key = _region_index_key(source_id)
    value = json.dumps(
        {
            "version": REGION_INDEX_VERSION,
            "complete": complete,
            "pages": pages,
            "updated_at": now_iso(),
        },
        ensure_ascii=False,
    )
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value, updated_at=now_iso()))
    else:
        row.value = value
        row.updated_at = now_iso()


def _existing_region_boxes(db: Session, source_id: str) -> tuple[set[tuple[int | None, tuple[int, ...]]], int]:
    rows = db.execute(
        select(Evidence).where(
            Evidence.source_id == source_id,
            Evidence.kind == "image_region",
            Evidence.status == "active",
        )
    ).scalars().all()
    existing: set[tuple[int | None, tuple[int, ...]]] = set()
    for row in rows:
        try:
            locator = json.loads(row.locator_json or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        for box in locator.get("boxes") or []:
            try:
                existing.add((row.page_no, _region_box_key(box)))
            except (TypeError, ValueError):
                continue
    max_ordinal = db.scalar(
        select(func.max(Evidence.ordinal)).where(Evidence.source_id == source_id)
    )
    return existing, int(max_ordinal or 0)


def _ensure_pdf_region_evidence_locked(db: Session, source: SourceMaterial | None) -> int:
    """为旧资料或懒加载资料补建内嵌图区域证据；按页/区域框去重，可重复执行。"""
    if source is None or source.kind != "pdf" or source.status != "active":
        return 0
    if _region_index_complete(db, source.id):
        return 0
    path = storage.absolute(source.stored_path)
    if not path.exists():
        logger.warning("pdf region index skipped source=%s reason=source_missing", source.id)
        return 0
    try:
        doc = pdf_engine.open_document(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pdf region index skipped source=%s reason=open_failed error=%s",
            source.id,
            type(exc).__name__,
        )
        return 0

    existing, ordinal = _existing_region_boxes(db, source.id)
    added = 0
    pages = 0
    complete = True
    try:
        for index in range(doc.page_count):
            page = doc[index]
            page_no = index + 1
            pages += 1
            try:
                boxes = pdf_engine.page_image_boxes(page)
            except Exception as exc:  # noqa: BLE001
                complete = False
                logger.warning(
                    "pdf region index page failed source=%s page=%s error=%s",
                    source.id,
                    page_no,
                    type(exc).__name__,
                )
                continue
            for box in boxes:
                try:
                    key = (page_no, _region_box_key(box))
                except (TypeError, ValueError):
                    continue
                if key in existing:
                    continue
                ordinal += 1
                _register_evidence(
                    db,
                    source.id,
                    "image_region",
                    page_no,
                    ordinal,
                    {"boxes": [box], "deferred": True},
                    quote=f"{source.display_name} 第 {page_no} 页内嵌图片区域",
                )
                existing.add(key)
                added += 1
        db.flush()
    except Exception as exc:  # noqa: BLE001
        complete = False
        logger.warning(
            "pdf region index failed source=%s error=%s", source.id, type(exc).__name__
        )
    finally:
        doc.close()
    if complete:
        _write_region_index(db, source.id, complete=True, pages=pages)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(
            "pdf region index commit failed source=%s error=%s",
            source.id,
            type(exc).__name__,
        )
    logger.info(
        "pdf region index source=%s added=%s pages=%s complete=%s",
        source.id,
        added,
        pages,
        complete,
    )
    return added


def _ensure_page_image_locked(db: Session, source: SourceMaterial | None, page_no: int | None):
    """按需渲染 PDF 页图并复用；失败返回 None，由调用方跳过该图或降级文字。"""
    if source is None or source.kind != "pdf" or page_no is None:
        return None
    page = db.get(SourcePage, (source.id, page_no))
    if page is None:
        return None
    if page.page_image_asset_id:
        asset = db.get(ImageAsset, page.page_image_asset_id)
        if asset is not None and asset.source_id == source.id:
            try:
                if storage.absolute(asset.rel_path).exists():
                    return asset
            except Exception:  # noqa: BLE001
                pass
    path = storage.absolute(source.stored_path)
    if not path.exists():
        logger.warning("pdf page render skipped source=%s page=%s reason=source_missing", source.id, page_no)
        return None
    try:
        doc = pdf_engine.open_document(str(path))
        try:
            page_png = pdf_engine.render_page(doc[page_no - 1])
        finally:
            doc.close()
        asset_id = new_id("asset")
        page_path = storage.pages_dir(source.id) / f"p{page_no:04d}.png"
        storage.atomic_write(page_path, page_png)
        asset = ImageAsset(
            id=asset_id,
            source_id=source.id,
            kind="pdf_page_render",
            page_no=page_no,
            rel_path=storage.relative(page_path),
            mime="image/png",
            width=0,
            height=0,
            size_bytes=len(page_png),
            sha256=image_engine.sha256(page_png),
        )
        db.add(asset)
        page.page_image_asset_id = asset_id
        db.flush()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "pdf page render failed source=%s page=%s error=%s",
            source.id,
            page_no,
            type(exc).__name__,
        )
        return None
    logger.info("pdf page rendered on demand source=%s page=%s", source.id, page_no)
    return asset


def ensure_pdf_region_evidence(db: Session, source: SourceMaterial | None) -> int:
    with _SOURCE_VISUAL_LOCK:
        return _ensure_pdf_region_evidence_locked(db, source)


def ensure_page_image(db: Session, source: SourceMaterial | None, page_no: int | None):
    with _SOURCE_VISUAL_LOCK:
        return _ensure_page_image_locked(db, source, page_no)


def licence_mime(ext: str) -> str:
    return MIME_BY_EXT.get(ext, "application/octet-stream")