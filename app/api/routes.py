from __future__ import annotations

import json
import math
import os
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, settings
from app.domain import diagnosis
from app.domain import assessment as assessment_service
from app.domain import brief as brief_service
from app.domain import evidence as evidence_service
from app.domain import mastery
from app.domain import knowledge_base as kb_service
from app.domain import plans as plan_service
from app.domain import schedule as schedule_service
from app.domain.deletion import (
    DeleteError,
    affected_packs,
    citation_in_pack,
    delete_pack,
    delete_source,
    locator_valid,
    source_status_map,
)
from app.domain.generation import create_job, create_review_focus, start_job
from app.domain.grading import grade
from app.domain.knowledge import rename_knowledge_point
from app.domain.models import (
    AppSetting,
    Attempt,
    AttemptAnswer,
    Citation,
    DiagnosticSession,
    Evidence,
    GenerationJob,
    HandoutSection,
    ImageAsset,
    KnowledgePoint,
    LearningPack,
    LearningPlan,
    LearningPlanVersion,
    PackPlanVersion,
    PackSource,
    ProgressSnapshot,
    Question,
    QuestionKnowledgePoint,
    ReviewFocus,
    ScheduleTask,
    SourceMaterial,
    SourcePage,
    now_iso,
)
from app.domain.schemas import (
    AssessmentRequest,
    ConfirmRequest,
    PackPositionRequest,
    CreatePackRequest,
    DraftRequest,
DiagnosisAnswerRequest,
    DiagnosisConfirmRequest,
    DiagnosisPlanRequest,
    DiagnosisStartRequest,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseItemAddRequest,
    KnowledgeBaseItemUpdateRequest,
    KnowledgeBaseUpdateRequest,
    KnowledgePointRename,
    PlanCreateRequest,
    PlanScheduleRequest,
    PlanVersionRequest,
    ReviewFocusItem,
    ReviewFocusText,
    SchedulePreviewRequest,
    ScheduleReschedulePreviewRequest,
    ScheduleRescheduleRequest,
    ScheduleShiftRequest,
    SourceSpec,
    SubmitRequest,
    SuggestPlanRequest,
)
from app.infra import pdf_engine, source_service, storage
from app.infra import workspace as workspace_service
from app.infra.db import SCHEMA_VERSION, SessionLocal, engine
from app.infra.ids import new_id
from app.infra.migrations import read_schema_state
from app.infra.source_service import ImportError as SourceImportError

router = APIRouter(prefix="/api")
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))

MB = 1024 * 1024


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def fail(status: int, code: str, message: str, field: str | None = None, actions: list[str] | None = None):
    payload: dict = {"error": {"code": code, "message": message, "actions": actions or []}}
    if field:
        payload["error"]["field"] = field
    return HTTPException(status_code=status, detail=payload)


def _file_limit(ext: str) -> int:
    if ext == "pdf":
        return 50 * MB
    if ext in {"png", "jpg", "jpeg", "webp"}:
        return 20 * MB
    return 5 * MB


@router.get("/health")
def health():
    state = read_schema_state(engine)
    current = workspace_service.current_user()
    return {
        "status": "ok",
        "app": "xunzhi",
        "db": "ok",
        "schema_version": state.get("schema_version") or SCHEMA_VERSION,
        "migration": state,
        "instance_id": os.getenv("XUNZHI_INSTANCE_ID", "standalone"),
        "user_id": current["id"],
        "user_name": current["display_name"],
        "desktop_mode": settings.desktop_mode,
    }


@router.get("/settings/model-status")
def model_status():
    return {
        "configured": settings.key_configured,
        "model": settings.model,
        "can_generate": settings.can_generate,
        "fake_mode": settings.is_fake,
        "is_test_double": settings.is_fake,
    }


def serialize_source(item: SourceMaterial) -> dict:
    return {
        "id": item.id,
        "display_name": item.display_name,
        "kind": item.kind,
        "ext": item.ext,
        "size_bytes": item.size_bytes,
        "page_count": item.page_count,
        "status": item.status,
        "created_at": item.created_at,
    }


@router.post("/sources", status_code=201)
async def upload_source(file: UploadFile, db: Session = Depends(get_db)):
    filename = file.filename or "unnamed"
    ext = Path(filename).suffix.lower().lstrip(".")
    data = await file.read()
    if len(data) > _file_limit(ext):
        raise fail(413, "too_large", f"该文件超过 {_file_limit(ext) // MB} MB 上限")

    try:
        source, reused = source_service.import_source(db, filename, data)
        db.commit()
    except SourceImportError as exc:
        db.rollback()
        code = 415 if exc.code == "unsupported_format" else 422
        raise fail(code, exc.code, exc.message)
    except Exception:
        db.rollback()
        raise fail(500, "storage_error", "文件保存失败，请重试")
    return {"source": serialize_source(source), "reused": reused}


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    rows = db.execute(
        select(SourceMaterial).where(SourceMaterial.status == "active").order_by(SourceMaterial.created_at)
    ).scalars().all()
    items = []
    for row in rows:
        count = db.execute(
            select(PackSource)
            .join(LearningPack, LearningPack.id == PackSource.pack_id)
            .where(PackSource.source_id == row.id, LearningPack.source_kind != "diagnostic")
        ).scalars().all()
        payload = serialize_source(row)
        payload["referenced_pack_count"] = len(count)
        items.append(payload)
    return {
        "items": items,
        "limits": {
            "max_sources": settings.max_sources,
            "max_pages": settings.max_pages,
            "max_total_mb": settings.max_total_mb,
            "max_progress_chars": settings.max_progress_chars,
        },
    }


@router.get("/sources/{source_id}/impact")
def source_impact(source_id: str, db: Session = Depends(get_db)):
    source = db.get(SourceMaterial, source_id)
    if source is None or source.status != "active":
        raise fail(404, "not_found", "资料不存在")
    rows = db.execute(
        select(PackSource, LearningPack)
        .join(LearningPack, LearningPack.id == PackSource.pack_id)
        .where(PackSource.source_id == source_id, LearningPack.source_kind != "diagnostic")
    ).all()
    packs = [
        {"id": pack.id, "title": pack.display_title, "created_at": pack.created_at}
        for _link, pack in rows
    ]
    return {"referenced_pack_count": len(packs), "packs": packs}


@router.delete("/sources/{source_id}")
def remove_source(source_id: str, payload: ConfirmRequest, db: Session = Depends(get_db)):
    if not payload.confirm:
        raise fail(409, "confirm_required", "删除原始资料需要确认影响范围")
    try:
        return delete_source(db, source_id)
    except DeleteError as exc:
        raise fail(exc.status, exc.code, exc.message)


def _kb_error(exc: Exception):
    if isinstance(exc, kb_service.KnowledgeBaseError):
        return fail(exc.status, exc.code, exc.message)
    return fail(422, "knowledge_base_error", str(exc))


@router.get("/knowledge-bases")
def list_knowledge_bases(db: Session = Depends(get_db)):
    return {
        "items": kb_service.list_bases(db),
        "limits": {
            "kb_max_sources": settings.kb_max_sources,
            "kb_max_pages": settings.kb_max_pages,
        },
        "model_budget": {
            "max_text_items": settings.model_max_text_items,
            "max_images": settings.model_max_images,
            "max_request_mb": settings.model_max_request_mb,
        },
    }


@router.post("/knowledge-bases", status_code=201)
def create_knowledge_base(payload: KnowledgeBaseCreateRequest, db: Session = Depends(get_db)):
    try:
        return kb_service.create_base(db, payload.name, payload.description)
    except Exception as exc:  # noqa: BLE001
        raise _kb_error(exc)


@router.get("/knowledge-bases/{kb_id}")
def get_knowledge_base(kb_id: str, db: Session = Depends(get_db)):
    try:
        return kb_service.serialize_base(db, kb_service._get_base(db, kb_id))
    except Exception as exc:  # noqa: BLE001
        raise _kb_error(exc)


@router.put("/knowledge-bases/{kb_id}")
def update_knowledge_base(
    kb_id: str, payload: KnowledgeBaseUpdateRequest, db: Session = Depends(get_db)
):
    try:
        return kb_service.update_base(
            db, kb_id, name=payload.name, description=payload.description
        )
    except Exception as exc:  # noqa: BLE001
        raise _kb_error(exc)


@router.delete("/knowledge-bases/{kb_id}")
def remove_knowledge_base(
    kb_id: str, payload: ConfirmRequest, db: Session = Depends(get_db)
):
    if not payload.confirm:
        raise fail(409, "confirm_required", "删除知识库需要确认；原文件不会被删除")
    try:
        return kb_service.delete_base(db, kb_id)
    except Exception as exc:  # noqa: BLE001
        raise _kb_error(exc)


@router.post("/knowledge-bases/{kb_id}/items", status_code=201)
def add_existing_source(
    kb_id: str, payload: KnowledgeBaseItemAddRequest, db: Session = Depends(get_db)
):
    try:
        return kb_service.add_existing_source(db, kb_id, payload.source_id, payload.note)
    except Exception as exc:  # noqa: BLE001
        raise _kb_error(exc)


@router.post("/knowledge-bases/{kb_id}/items/upload", status_code=202)
async def upload_knowledge_base_items(
    kb_id: str,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    errors = []
    for file in files:
        filename = file.filename or "unnamed"
        data = await file.read()
        ext = Path(filename).suffix.lower().lstrip(".")
        if len(data) > _file_limit(ext):
            errors.append({"name": filename, "message": "文件超过大小上限"})
            continue
        try:
            kb_service.add_upload(db, kb_id, filename, data)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "name": filename,
                    "message": getattr(exc, "message", str(exc)),
                }
            )
    payload = kb_service.serialize_base(db, kb_service._get_base(db, kb_id))
    payload["upload_errors"] = errors
    return payload


@router.patch("/knowledge-bases/{kb_id}/items/{item_id}")
def update_knowledge_base_item(
    kb_id: str,
    item_id: str,
    payload: KnowledgeBaseItemUpdateRequest,
    db: Session = Depends(get_db),
):
    try:
        return kb_service.update_item(
            db, kb_id, item_id, display_name=payload.display_name, note=payload.note
        )
    except Exception as exc:  # noqa: BLE001
        raise _kb_error(exc)


@router.post("/knowledge-bases/{kb_id}/items/{item_id}/replace", status_code=202)
async def replace_knowledge_base_item(
    kb_id: str,
    item_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    data = await file.read()
    try:
        return kb_service.replace_item_upload(
            db, kb_id, item_id, file.filename or "replacement", data
        )
    except Exception as exc:  # noqa: BLE001
        raise _kb_error(exc)


@router.delete("/knowledge-bases/{kb_id}/items/{item_id}")
def remove_knowledge_base_item(
    kb_id: str, item_id: str, db: Session = Depends(get_db)
):
    try:
        return kb_service.remove_item(db, kb_id, item_id)
    except Exception as exc:  # noqa: BLE001
        raise _kb_error(exc)


@router.post("/packs", status_code=202)
def create_pack(payload: CreatePackRequest, db: Session = Depends(get_db)):
    if not payload.consent_ack:
        raise fail(422, "consent_required", "请先确认本次将发送给模型服务的内容", field="consent_ack")
    if not payload.progress_text.strip():
        raise fail(422, "progress_required", "写一句你学到哪、哪里不懂即可", field="progress_text")
    if len(payload.progress_text) > settings.max_progress_chars:
        raise fail(422, "progress_too_long", f"进度描述不超过 {settings.max_progress_chars} 字", field="progress_text")
    if not settings.can_generate:
        raise fail(409, "no_api_key", "模型服务未配置，可先用离线示例体验完整流程")

    parent: LearningPack | None = None
    parent_label: str | None = None
    focus_points: list[dict] = []
    if payload.parent_pack_id:
        parent = db.get(LearningPack, payload.parent_pack_id)
        if parent is None:
            raise fail(404, "parent_not_found", "上一轮学习计划不存在")
        parent_attempt = _latest_submitted(db, parent.id)
        if parent_attempt is None:
            raise fail(409, "parent_not_submitted", "上一轮尚未提交，无法针对错题再练")
        if not payload.review_focus:
            raise fail(422, "review_focus_required", "至少保留 1 条复习重点，或返回按原错题再练")
        if len(payload.review_focus) > 5:
            raise fail(422, "review_focus_limit", "复习重点最多 5 条，请先合并")
        parent_links = db.execute(
            select(PackSource).where(PackSource.pack_id == parent.id)
        ).scalars().all()
        if not parent_links:
            raise fail(409, "parent_not_available", "上一轮资料已不可用，请重新选择")
        missing = []
        for link in parent_links:
            source = db.get(SourceMaterial, link.source_id)
            if source is None or source.status != "active":
                missing.append(link.source_name_snapshot)
        if missing:
            raise fail(
                409,
                "source_deleted",
                "原资料已删除，需重新选择或上传：" + "、".join(missing),
                actions=["reselect_source", "upload_source"],
            )
        if not payload.sources:
            payload.sources = [
                SourceSpec(
                    source_id=link.source_id,
                    page_start=link.page_start,
                    page_end=link.page_end,
                )
                for link in parent_links
            ]
        parent_label = parent.display_title
        focus_rows = db.execute(
            select(ReviewFocus)
            .where(
                ReviewFocus.source_pack_id == parent.id,
                ReviewFocus.source_attempt_id == parent_attempt.id,
                ReviewFocus.is_deleted.is_(False),
            )
            .order_by(ReviewFocus.ordinal)
        ).scalars().all()
        for row in focus_rows:
            point_id = row.knowledge_point_id
            if point_id is None and row.question_id:
                link = db.execute(
                    select(QuestionKnowledgePoint).where(
                        QuestionKnowledgePoint.question_id == row.question_id,
                        QuestionKnowledgePoint.role == "primary",
                    )
                ).scalars().first()
                point_id = link.knowledge_point_id if link is not None else None
            focus_points.append(
                {
                    "knowledge_point_id": point_id,
                    "display_name": row.edited_text or row.knowledge_point,
                    "source_question_id": row.question_id or None,
                }
            )

    if not payload.sources:
        raise fail(422, "source_required", "请先上传或选择至少 1 份资料", field="sources")

    spec_ids = [spec.source_id for spec in payload.sources]
    if len(set(spec_ids)) != len(spec_ids):
        raise fail(422, "duplicate_source", "同一份资料不能重复选择，请移除重复项", field="sources")
    if len(spec_ids) > settings.max_sources:
        raise fail(
            413,
            "too_many_sources",
            f"单轮最多选择 {settings.max_sources} 份资料，请移除多余资料",
            field="sources",
        )

    scope_parts: list[str] = []
    sources_payload: list[dict] = []
    total_bytes = 0
    total_pages = 0
    for spec in payload.sources:
        source = db.get(SourceMaterial, spec.source_id)
        if source is None or source.status != "active":
            raise fail(409, "source_deleted", "所选资料已删除，需重新选择或上传", field="sources")
        total_bytes += source.size_bytes
        if source.kind == "pdf":
            try:
                pdf_engine.validate_range(spec.page_start, spec.page_end, source.page_count or 1, settings.max_pages)
            except ValueError as exc:
                raise fail(422, "invalid_page_range", str(exc), field="sources")
            start = spec.page_start or 1
            end = spec.page_end or (source.page_count or 1)
            total_pages += end - start + 1
            scope_parts.append(
                f"{source.display_name} 第 {spec.page_start or 1}–{spec.page_end or source.page_count} 页"
            )
        else:
            scope_parts.append(source.display_name)
        sources_payload.append(
            {
                "source_id": source.id,
                "page_start": spec.page_start,
                "page_end": spec.page_end,
            }
        )

    if total_bytes > settings.max_total_mb * MB:
        raise fail(
            413,
            "too_large",
            f"本轮所选资料总大小超过 {settings.max_total_mb} MB 上限，请移除资料",
            field="sources",
        )
    if total_pages > settings.max_pages:
        raise fail(
            413,
            "too_large",
            f"本轮所选 PDF 合计 {total_pages} 页，超过单轮 {settings.max_pages} 页上限，请缩小页码范围",
            field="sources",
        )

    plan_constraints = None
    if payload.plan_version_id:
        version = db.get(LearningPlanVersion, payload.plan_version_id)
        if version is None:
            raise fail(404, "plan_version_not_found", "计划版本不存在")
        if version.status != "confirmed":
            raise fail(409, "plan_version_not_confirmed", "只有已确认的计划版本才能用于生成")
        if not plan_service.sources_match(json.loads(version.sources_json or "[]"), sources_payload):
            raise fail(409, "plan_version_mismatch", "计划版本适用的资料与本次选择不一致")
        if version.question_count:
            if payload.question_count is None:
                payload.question_count = version.question_count
            elif payload.question_count != version.question_count:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": {
                            "code": "plan_question_count_mismatch",
                            "message": "本次请求题量与已确认计划的总题量不一致",
                            "request_question_count": payload.question_count,
                            "plan_question_count": version.question_count,
                            "field": "question_count",
                            "actions": ["use_plan_count", "edit_plan"],
                        }
                    },
                )
        try:
            plan_constraints = plan_service.build_generation_constraints(
                db, version, sources_payload
            )
        except ValueError as exc:
            code = str(exc)
            if code == "plan_allocation_invalid":
                raise fail(422, code, "计划题量分配合计与总题量不一致，请先修改计划", field="items")
            if code == "plan_excluded_point":
                raise fail(422, code, "计划包含已标记暂未学到的考点", field="items")
            if code == "plan_modules_missing":
                raise fail(
                    409,
                    code,
                    "已确认计划缺少可用的学习模块，请返回诊断重新生成方案",
                    actions=["replan"],
                )
            if code == "plan_point_ambiguous":
                raise fail(
                    409,
                    code,
                    "原计划无法区分部分考点，需重新制定学习计划",
                    actions=["replan"],
                )
            raise

    if payload.question_count is None:
        payload.question_count = 5

    request_payload = {
        "idempotency_key": payload.idempotency_key,
        "sources": sources_payload,
        "progress_text": payload.progress_text,
        "difficulty": payload.difficulty,
        "question_count": payload.question_count,
        "review_focus": payload.review_focus,
        "focus_points": focus_points,
        "parent_pack_id": payload.parent_pack_id,
        "parent_label": parent_label,
        "plan_version_id": payload.plan_version_id,
        "plan_constraints": plan_constraints,
        "scope_text": "；".join(scope_parts),
    }
    job, duplicate = create_job(db, request_payload)
    if not duplicate:
        start_job(job.id)
    result = {
        "job_id": job.id,
        "status": job.status,
        "stage": job.stage,
        "created_at": job.created_at,
        "duplicate": duplicate,
        "is_test_double": settings.is_fake,
    }
    return JSONResponse(status_code=200 if duplicate else 202, content=result)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise fail(404, "not_found", "生成任务不存在")
    elapsed = 0
    try:
        created = datetime.fromisoformat(job.created_at.replace("Z", "+00:00"))
        elapsed = int((datetime.now(created.tzinfo) - created).total_seconds() * 1000)
    except ValueError:
        pass
    payload = {
        "job_id": job.id,
        "status": job.status,
        "stage": job.stage,
        "stage_updated_at": job.stage_updated_at,
        "elapsed_ms": elapsed,
        "pack_id": job.pack_id,
        "is_test_double": settings.is_fake,
    }
    is_diagnostic = (job.idempotency_key or "").startswith("diagnostic:")
    attempt_no = diagnosis._attempt_from_key(job.idempotency_key) if is_diagnostic else 0
    replan_required = diagnosis.replan_required_for_job(db, job)
    retry_available = False
    if is_diagnostic:
        if job.status == "queued":
            retry_available = job.launch_state in (None, "pending_dispatch")
        elif job.status in diagnosis.JOB_FAILED_STATUSES:
            retry_available = attempt_no < diagnosis.MAX_GENERATION_ATTEMPTS
    if replan_required:
        retry_available = False
    payload["attempt_no"] = attempt_no
    payload["retry_available"] = retry_available
    payload["actions"] = ["replan"] if replan_required else []
    if job.error_code:
        payload["error"] = {
            "code": job.error_code,
            "message": (
                "原计划无法区分部分考点，需重新制定学习计划"
                if replan_required
                else (job.error_detail or "")
            ),
            "actions": ["replan"] if replan_required else ["retry", "edit"],
            "retryable": False if replan_required else retry_available,
        }
    return payload


@router.post("/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    result = db.execute(
        update(GenerationJob)
        .where(
            GenerationJob.id == job_id,
            GenerationJob.status.in_(("queued", "running")),
        )
        .values(status="cancelled", updated_at=now_iso())
    )
    db.commit()
    if result.rowcount != 1:
        job = db.get(GenerationJob, job_id)
        if job is None:
            raise fail(404, "not_found", "生成任务不存在")
        raise fail(409, "job_finished", "任务已结束，无法取消")
    return {"status": "cancelled"}


def _valid_box(box) -> bool:
    return (
        isinstance(box, list)
        and len(box) == 4
        and all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and 0 <= value <= 1
            for value in box
        )
        and box[2] > box[0]
        and box[3] > box[1]
    )


def _safe_locator(raw: str | None) -> dict:
    try:
        locator = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return locator if isinstance(locator, dict) else {}


def _locator_boxes(locator: dict) -> list[list[float]]:
    box_2d = locator.get("box_2d")
    if _valid_box(box_2d):
        return [box_2d]
    boxes = locator.get("boxes")
    if isinstance(boxes, list) and boxes:
        return [box for box in boxes if _valid_box(box)]
    rects = locator.get("rects")
    if isinstance(rects, list) and rects:
        return [box for box in rects if _valid_box(box)]
    rect = locator.get("rect")
    if _valid_box(rect):
        return [rect]
    return []


def _resolve_citation_media(db: Session, citation: Citation, source: SourceMaterial, evidence: Evidence | None):
    locator = _safe_locator(citation.locator_json)
    asset = None
    asset_kind = None
    scanned = False

    if evidence is not None and evidence.kind == "text_span":
        if source.kind == "pdf" and citation.page_no is not None:
            page = db.get(SourcePage, (source.id, citation.page_no))
            if page is not None and page.page_image_asset_id:
                asset = db.get(ImageAsset, page.page_image_asset_id)
                asset_kind = "page_image"
                scanned = bool(page.is_scanned)
    else:
        asset_id = locator.get("asset_id")
        asset = db.get(ImageAsset, asset_id) if asset_id else None
        if asset is None and source.kind == "pdf" and citation.page_no is not None:
            asset = source_service.ensure_page_image(db, source, citation.page_no)
            if asset is not None:
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                    asset = None
        asset_kind = evidence.kind if evidence else None
        scanned = bool(locator.get("scanned"))
        if citation.page_no is not None and asset is not None and asset.page_no is None:
            page = db.get(SourcePage, (source.id, citation.page_no))
            if page is not None:
                scanned = bool(page.is_scanned)

    if asset is not None and asset.source_id != source.id:
        asset = None
    if (
        asset is not None
        and citation.page_no is not None
        and asset.page_no is not None
        and asset.page_no != citation.page_no
    ):
        asset = None
    if citation.page_no is not None:
        page = db.get(SourcePage, (source.id, citation.page_no))
        if page is not None:
            scanned = scanned or bool(page.is_scanned)
    return asset, asset_kind, scanned


def _citation_payload(db: Session, citation: Citation) -> dict:
    source = db.get(SourceMaterial, citation.source_id)
    evidence = db.get(Evidence, citation.evidence_id)
    locator = _safe_locator(citation.locator_json)
    boxes = _locator_boxes(locator)
    kind = evidence.kind if evidence else "text_span"
    asset, asset_kind, scanned = (None, None, False)
    if source is not None and evidence is not None:
        asset, asset_kind, scanned = _resolve_citation_media(db, citation, source, evidence)
    asset_path_ok = False
    if asset is not None:
        try:
            asset_path_ok = storage.absolute(asset.rel_path).exists()
        except Exception:
            asset_path_ok = False
    if asset is not None and not asset_path_ok:
        asset = None
    if asset is not None and not (asset.mime or "").startswith("image/"):
        asset = None
    asset_expected = bool(
        source is not None
        and evidence is not None
        and (
            kind in {"page_image", "image_region"}
            or (source.kind == "pdf" and kind == "text_span")
        )
    )
    image_description = (locator.get("image_description") or "").strip() or None
    if image_description:
        text_alternative = image_description
        text_alternative_source = "image_description"
    elif citation.claim_text:
        text_alternative = citation.claim_text
        text_alternative_source = "claim"
    elif citation.quote_snapshot:
        text_alternative = citation.quote_snapshot
        text_alternative_source = "quote"
    else:
        text_alternative = "资料引用"
        text_alternative_source = "none"
    label = {
        "text_span": f"第 {citation.page_no} 页 · 原文" if citation.page_no else "原文片段",
        "page_image": f"第 {citation.page_no} 页 · 整页",
        "image_region": "拓扑图 · 区域 1 / 1" if not citation.page_no else f"第 {citation.page_no} 页 · 图像区域",
    }.get(kind, "资料引用")
    if source is not None and source.kind == "pdf" and kind == "text_span" and scanned:
        label += "（扫描页）"
    return {
        "id": citation.id,
        "local_ref": citation.local_ref,
        "evidence_id": citation.evidence_id,
        "source_id": citation.source_id,
        "kind": kind,
        "asset_kind": asset_kind,
        "label": label,
        "claim": citation.claim_text,
        "source_name": source.display_name if source else "来源资料已删除",
        "source_status": source.status if source else "deleted",
        "page_no": citation.page_no,
        "page_count": source.page_count if source else None,
        "boxes": boxes,
        "scanned": scanned,
        "quote": citation.quote_snapshot,
        "image_description": image_description,
        "text_alternative": text_alternative,
        "text_alternative_source": text_alternative_source,
        "asset_url": f"/api/citations/{citation.id}/asset" if asset is not None else None,
        "asset_expected": asset_expected,
        "asset_missing": asset_expected and asset is None,
        "text_only": not asset_expected,
    }


def _attempt_answers(db: Session, attempt_id: str) -> dict[str, int | None]:
    rows = db.execute(select(AttemptAnswer).where(AttemptAnswer.attempt_id == attempt_id)).scalars().all()
    return {row.question_id: row.selected_index for row in rows}


def _latest_submitted(
    db: Session, pack_id: str, task_id: str | None = None
) -> Attempt | None:
    query = select(Attempt).where(
        Attempt.pack_id == pack_id,
        Attempt.status == "submitted",
    )
    if task_id:
        query = query.where(Attempt.schedule_task_id == task_id)
    else:
        query = query.where(Attempt.schedule_task_id.is_(None))
    return db.execute(
        query.order_by(Attempt.submitted_at.desc(), Attempt.id.desc())
    ).scalars().first()


POSITIONS_KEY = "learning.positions"
LAST_LEARNING_KEY = "learning.last"
VIEW_LABELS = {
    "handout": "讲解",
    "modules": "模块讲解",
    "review": "综合复习",
    "questions": "练习",
}


def _setting_json(db: Session, key: str) -> dict:
    row = db.get(AppSetting, key)
    if row is None or not row.value:
        return {}
    try:
        value = json.loads(row.value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_setting(db: Session, key: str, value: dict) -> None:
    row = db.get(AppSetting, key)
    encoded = json.dumps(value, ensure_ascii=False)
    if row is None:
        db.add(AppSetting(key=key, value=encoded, updated_at=now_iso()))
    else:
        row.value = encoded
        row.updated_at = now_iso()


def _pack_position(db: Session, pack_id: str) -> dict:
    positions = _setting_json(db, POSITIONS_KEY)
    position = positions.get(pack_id)
    return position if isinstance(position, dict) else {}


def _pack_modules(db: Session, pack_id: str) -> list[dict]:
    adoption = db.get(PackPlanVersion, pack_id)
    if adoption is None:
        return []
    version = db.get(LearningPlanVersion, adoption.plan_version_id)
    if version is None:
        return []
    try:
        modules = json.loads(version.modules_json or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return modules if isinstance(modules, list) else []


def _schedule_options(payload) -> dict:
    return {
        "start_date": getattr(payload, "start_date", None),
        "duration_days": getattr(payload, "duration_days", None),
        "target_date": getattr(payload, "target_date", None),
        "daily_minutes": getattr(payload, "daily_minutes", None),
    }


GENERIC_PLAN_TITLES = {"学习计划", "本次学习计划", "知识库学习方案", "本次学习路线"}


def _plan_display_title(plan: LearningPlan, pack: LearningPack | None) -> str:
    title = (plan.title or "").strip()
    if (not title or title in GENERIC_PLAN_TITLES) and pack is not None:
        return (pack.display_title or "").strip() or title or "学习计划"
    return title or "学习计划"


def _schedule_http_error(exc: schedule_service.ScheduleError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail={"error": {"code": exc.code, "message": exc.message, "actions": []}},
    )


def _schedule_version(
    db: Session, plan_id: str | None, version_id: str | None
) -> tuple[LearningPlan, LearningPlanVersion]:
    plan = None
    version = None
    if plan_id:
        plan = db.get(LearningPlan, plan_id)
        if plan is None:
            raise fail(404, "not_found", "学习计划不存在")
    if version_id:
        version = db.get(LearningPlanVersion, version_id)
        if version is None:
            raise fail(404, "plan_version_not_found", "学习计划版本不存在")
        if plan is not None and version.plan_id != plan.id:
            raise fail(404, "plan_version_not_found", "学习计划版本不存在")
        if plan is None:
            plan = db.get(LearningPlan, version.plan_id)
    elif plan is not None:
        version = schedule_service.active_version(db, plan)
    if plan is None or version is None:
        raise fail(422, "schedule_plan_required", "需要指定学习计划")
    return plan, version


def _pack_schedule(db: Session, pack: LearningPack, today: date | None = None) -> dict:
    adoption = db.get(PackPlanVersion, pack.id)
    if adoption is None:
        return {
            "assigned": False,
            "plan_id": None,
            "plan_title": None,
            "version_id": None,
            "progress": schedule_service.progress_payload([], today),
            "schedule": {},
        }
    version = db.get(LearningPlanVersion, adoption.plan_version_id)
    plan = db.get(LearningPlan, version.plan_id) if version is not None else None
    tasks = schedule_service.tasks_for_version(db, version.id) if version is not None else []
    return {
        "assigned": bool(tasks),
        "plan_id": plan.id if plan is not None else None,
        "plan_title": plan.title if plan is not None else None,
        "version_id": version.id if version is not None else None,
        "progress": schedule_service.progress_payload(tasks, today),
        "schedule": schedule_service.schedule_config(version) if version is not None else {},
    }


def _recent_pack(db: Session) -> LearningPack | None:
    last = _setting_json(db, LAST_LEARNING_KEY)
    if last.get("pack_id"):
        pack = db.get(LearningPack, last.get("pack_id"))
        if pack is not None and pack.source_kind != "diagnostic":
            return pack
    positions = _setting_json(db, POSITIONS_KEY)
    ordered = sorted(
        positions.items(),
        key=lambda item: item[1].get("updated_at") or "",
        reverse=True,
    )
    for candidate_id, _ in ordered:
        candidate = db.get(LearningPack, candidate_id)
        if candidate is not None and candidate.source_kind != "diagnostic":
            return candidate
    return None


def _pack_page_index(db: Session, pack_id: str) -> dict[str, list[dict]]:
    sections = db.execute(
        select(HandoutSection)
        .where(
            HandoutSection.pack_id == pack_id,
            HandoutSection.section_type == "module_page",
        )
        .order_by(HandoutSection.ordinal)
    ).scalars().all()
    index: dict[str, list[dict]] = {}
    for section in sections:
        try:
            content = json.loads(section.content_json or "{}")
        except (TypeError, json.JSONDecodeError):
            content = {}
        index.setdefault(section.module_id or "", []).append(
            {
                "page_id": content.get("page_id") or section.id,
                "knowledge_point_ids": content.get("knowledge_point_ids") or [],
            }
        )
    return index


def _attach_task_pages(tasks: list[dict], page_index: dict[str, list[dict]]) -> None:
    for task in tasks:
        if not task.get("action_url") or task.get("task_type") == "practice":
            continue
        module_id = task.get("module_id") or ""
        if task.get("task_type") == "review":
            if module_id:
                task["action_url"] = f"{task['action_url']}&page_id=recap-{module_id}"
                task["page_target"] = "recap"
            continue
        pages = page_index.get(module_id) or []
        wanted = set(task.get("knowledge_point_ids") or [])
        match = next(
            (
                page
                for page in pages
                if wanted and wanted.intersection(page.get("knowledge_point_ids") or [])
            ),
            None,
        )
        page = match or (pages[0] if pages else None)
        if page is not None:
            task["action_url"] = f"{task['action_url']}&page_id={page['page_id']}"
            task["page_target"] = "page"


def _month_bounds(value: str | None, fallback: date) -> tuple[date, date]:
    if value:
        try:
            start = date.fromisoformat(f"{value}-01")
        except ValueError as exc:
            raise fail(422, "schedule_month_invalid", "月份需要 YYYY-MM 格式") from exc
    else:
        start = fallback.replace(day=1)
    end = start.replace(day=monthrange(start.year, start.month)[1])
    return start, end


def _clean_module_title(value: str | None, index: int = 0) -> str:
    text = (value or "").strip()
    text = re.sub(r"[、,，]\s*\d+/\d+(?![0-9/])", "", text)
    text = re.sub(r"^\d+/\d+\s*[、,，]?", "", text)
    text = re.sub(r"第\s*\d+\s*[-–—]\s*\d+\s*页", "", text)
    text = re.sub(r"[^\s、,，]{1,40}\.(?:pdf|txt|md|png|jpe?g|webp)(?![A-Za-z])", "", text, flags=re.IGNORECASE)
    text = text.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}", text):
        text = f"如何理解 {text}"
    if len(text) < 2:
        text = f"模块 {index + 1}"
    return text


def _module_title(modules: list[dict], module_id: str | None) -> str | None:
    for index, module in enumerate(modules):
        if module.get("module_id") == module_id:
            return _clean_module_title(module.get("title"), index)
    return None


def _draft(db: Session, pack_id: str, task_id: str | None = None) -> Attempt | None:
    query = select(Attempt).where(Attempt.pack_id == pack_id, Attempt.status == "draft")
    if task_id:
        query = query.where(Attempt.schedule_task_id == task_id)
    else:
        query = query.where(Attempt.schedule_task_id.is_(None))
    return db.execute(query).scalars().first()


def _practice_task(
    db: Session, pack: LearningPack, task_id: str, *, allow_empty: bool = False
) -> tuple[ScheduleTask, list[str]]:
    task = db.get(ScheduleTask, task_id)
    if task is None:
        raise fail(404, "schedule_task_not_found", "日程练习任务不存在")
    if task.task_type != "practice":
        raise fail(409, "task_not_practice", "该日程任务不是练习任务")
    adoption = db.get(PackPlanVersion, pack.id)
    if adoption is None or adoption.plan_version_id != task.plan_version_id:
        raise fail(409, "task_plan_mismatch", "该练习不属于当前学习计划")
    task_pack = schedule_service.pack_for_task(db, task, pack)
    if task_pack is not None and task_pack.id != pack.id:
        raise fail(409, "task_pack_mismatch", "该练习属于另一份学习内容，请回到学习日历重新进入")
    ids = schedule_service.ensure_task_question_ids(db, task, pack)
    if not ids and not allow_empty:
        raise fail(409, "task_questions_unavailable", "本组在本次学习内容中没有可对应题目")
    return task, ids


def _scoped_questions(
    questions: list[Question], ids: list[str] | None
) -> list[Question]:
    if ids is None:
        return questions
    allowed = set(ids)
    return [question for question in questions if question.id in allowed]


@router.get("/packs")
def list_packs(limit: int = 50, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 200))
    packs = db.execute(
        select(LearningPack)
        .where(LearningPack.source_kind != "diagnostic")
        .order_by(LearningPack.created_at.desc())
        .limit(limit)
    ).scalars().all()
    items = []
    for pack in packs:
        progress = db.get(ProgressSnapshot, pack.id)
        links = db.execute(select(PackSource).where(PackSource.pack_id == pack.id)).scalars().all()
        submitted = _latest_submitted(db, pack.id)
        draft = _draft(db, pack.id)
        if submitted is not None:
            attempt_status = f"已提交 {submitted.correct_count}/{submitted.total_count}"
        elif draft is not None:
            answered = sum(
                1 for value in _attempt_answers(db, draft.id).values() if value is not None
            )
            attempt_status = "未作答" if answered == 0 else f"未提交 {answered}/{pack.question_count}"
        else:
            attempt_status = "无作答"
        raw = progress.raw_text if progress else ""
        schedule_info = _pack_schedule(db, pack)
        items.append(
            {
                "id": pack.id,
                "display_title": pack.display_title,
                "created_at": pack.created_at,
                "source_kind": pack.source_kind,
                "difficulty": pack.difficulty,
                "question_count": pack.question_count,
                "source_count": len(links),
                "attempt_status": attempt_status,
                "progress_excerpt": raw[:40],
                "plan_id": schedule_info["plan_id"],
                "plan_title": schedule_info["plan_title"],
                "schedule": schedule_info,
            }
        )
    return {"items": items, "offline": {"available": True, "title": "计算机网络入门示例（离线）", "url": "/offline"}}


@router.get("/learning/last")
def last_learning(db: Session = Depends(get_db)):
    last = _setting_json(db, LAST_LEARNING_KEY)
    had_last = bool(last.get("pack_id"))
    pack = db.get(LearningPack, last.get("pack_id")) if last.get("pack_id") else None
    fallback = False
    if pack is None or pack.source_kind == "diagnostic":
        positions = _setting_json(db, POSITIONS_KEY)
        ordered = sorted(
            positions.items(),
            key=lambda item: item[1].get("updated_at") or "",
            reverse=True,
        )
        for candidate_id, _ in ordered:
            candidate = db.get(LearningPack, candidate_id)
            if candidate is not None and candidate.source_kind != "diagnostic":
                pack = candidate
                fallback = had_last
                break
        if pack is None:
            pack = db.execute(
                select(LearningPack)
                .where(LearningPack.source_kind != "diagnostic")
                .order_by(LearningPack.created_at.desc())
            ).scalars().first()
            fallback = had_last and pack is not None
    if pack is None:
        return {"available": False, "fallback": False}
    modules = _pack_modules(db, pack.id)
    position = _pack_position(db, pack.id)
    module_ids = {module.get("module_id") for module in modules if module.get("module_id")}
    module_id = position.get("module_id")
    page_id = position.get("page_id")
    if module_id not in module_ids:
        module_id = modules[0].get("module_id") if modules else None
    view = position.get("view") or ("modules" if modules else "handout")
    if not modules and view == "modules":
        view = "handout"
    submitted = _latest_submitted(db, pack.id)
    draft = _draft(db, pack.id)
    answered = 0
    if draft is not None:
        answered = sum(1 for value in _attempt_answers(db, draft.id).values() if value is not None)
    if submitted is not None:
        answer_state = "submitted"
        answer_label = f"已提交 {submitted.correct_count}/{submitted.total_count}"
        if not position.get("view"):
            view = "questions"
    elif draft is not None and answered:
        answer_state = "draft"
        answer_label = f"未提交 {answered}/{pack.question_count}"
    else:
        answer_state = "new"
        answer_label = "尚未作答"
    schedule_info = _pack_schedule(db, pack)
    return {
        "available": True,
        "fallback": fallback,
        "pack_id": pack.id,
        "pack_title": pack.display_title or "学习计划",
        "module_id": module_id,
        "page_id": page_id,
        "module_title": _module_title(modules, module_id),
        "view": view,
        "view_label": VIEW_LABELS.get(view, "学习"),
        "answer_state": answer_state,
        "answer_label": answer_label,
        "answered_count": answered,
        "question_count": pack.question_count,
        "url": f"/packs/{pack.id}",
        "updated_at": position.get("updated_at") or last.get("updated_at") or pack.created_at,
        "plan_id": schedule_info["plan_id"],
        "plan_title": schedule_info["plan_title"],
        "schedule": schedule_info,
        "task_progress_label": schedule_info["progress"]["label"],
    }


@router.put("/packs/{pack_id}/position")
def save_pack_position(
    pack_id: str,
    payload: PackPositionRequest,
    db: Session = Depends(get_db),
):
    pack = db.get(LearningPack, pack_id)
    if pack is None or pack.source_kind == "diagnostic":
        raise fail(404, "not_found", "学习计划不存在")
    positions = _setting_json(db, POSITIONS_KEY)
    now = now_iso()
    positions[pack_id] = {
        "module_id": payload.module_id,
        "page_id": payload.page_id,
        "view": payload.view,
        "updated_at": now,
    }
    if len(positions) > 100:
        ordered = sorted(
            positions.items(),
            key=lambda item: item[1].get("updated_at") or "",
            reverse=True,
        )[:100]
        positions = dict(ordered)
    _write_setting(db, POSITIONS_KEY, positions)
    _write_setting(
        db,
        LAST_LEARNING_KEY,
        {
            "pack_id": pack_id,
            "module_id": payload.module_id,
            "page_id": payload.page_id,
            "view": payload.view,
            "updated_at": now,
        },
    )
    db.commit()
    return {"ok": True, "pack_id": pack_id, **positions[pack_id]}


@router.delete("/packs/{pack_id}")
def remove_pack(pack_id: str, payload: ConfirmRequest, db: Session = Depends(get_db)):
    if not payload.confirm:
        raise fail(409, "confirm_required", "删除学习计划需要二次确认")
    try:
        result = delete_pack(db, pack_id)
    except DeleteError as exc:
        raise fail(exc.status, exc.code, exc.message)
    return result


@router.get("/packs/{pack_id}")
def get_pack(
    pack_id: str,
    task_id: str | None = None,
    db: Session = Depends(get_db),
):
    pack = db.get(LearningPack, pack_id)
    if pack is None or pack.source_kind == "diagnostic":
        raise fail(404, "not_found", "学习计划不存在")
    scope_task: ScheduleTask | None = None
    scope_ids: list[str] | None = None
    if task_id:
        scope_task, scope_ids = _practice_task(db, pack, task_id, allow_empty=True)
    progress = db.get(ProgressSnapshot, pack_id)
    sections = db.execute(
        select(HandoutSection).where(HandoutSection.pack_id == pack_id).order_by(HandoutSection.ordinal)
    ).scalars().all()
    questions = db.execute(
        select(Question).where(Question.pack_id == pack_id).order_by(Question.ordinal)
    ).scalars().all()
    citations = db.execute(select(Citation).where(Citation.pack_id == pack_id)).scalars().all()
    by_owner: dict[str, list[Citation]] = {}
    for citation in citations:
        by_owner.setdefault(citation.owner_id, []).append(citation)

    submitted = _latest_submitted(db, pack_id, task_id)
    if submitted is None and scope_task is not None and scope_task.attempt_id:
        candidate = db.get(Attempt, scope_task.attempt_id)
        if (
            candidate is not None
            and candidate.pack_id == pack_id
            and candidate.status == "submitted"
        ):
            submitted = candidate
    draft = _draft(db, pack_id, task_id)
    reveal = submitted is not None and draft is None
    scope_payload = (
        schedule_service.practice_scope_payload(db, pack, scope_task)
        if scope_task is not None
        else None
    )
    visible_questions = _scoped_questions(questions, scope_ids)

    handout = []

    def section_payload(section: HandoutSection) -> dict:
        return {
            "id": section.id,
            "section_type": section.section_type,
            "heading": section.heading,
            "body": section.body_text,
            "content": json.loads(section.content_json) if section.content_json else None,
            "is_extension": section.is_extension,
            "module_id": section.module_id,
            "citations": [_citation_payload(db, item) for item in by_owner.get(section.id, [])],
        }

    pages_by_module: dict[str, list[dict]] = {}
    recap_by_module: dict[str, dict] = {}
    for section in sections:
        if section.section_type == "module_page":
            payload = section_payload(section)
            content = payload.get("content") or {}
            visual = content.get("visual")
            if isinstance(visual, dict) and visual.get("citation_id"):
                citation_payload = next(
                    (item for item in payload["citations"] if item["id"] == visual["citation_id"]),
                    None,
                )
                if citation_payload is not None:
                    visual = {
                        **visual,
                        "asset_url": citation_payload.get("asset_url"),
                        "source_name": citation_payload.get("source_name"),
                        "source_status": citation_payload.get("source_status"),
                        "label": citation_payload.get("label"),
                        "page_no": citation_payload.get("page_no"),
                        "boxes": citation_payload.get("boxes"),
                        "asset_missing": citation_payload.get("asset_missing"),
                    }
            payload["page_id"] = content.get("page_id") or section.id
            payload["title"] = section.heading or content.get("title") or "学习页"
            payload["explanation"] = section.body_text or content.get("explanation") or ""
            payload["knowledge_point_ids"] = content.get("knowledge_point_ids") or []
            payload["page_type"] = content.get("page_type") or "text"
            payload["visual"] = visual
            pages_by_module.setdefault(section.module_id, []).append(payload)
        elif section.section_type == "module_recap":
            payload = section_payload(section)
            payload["recap"] = payload.get("content") or {}
            recap_by_module[section.module_id] = payload
        else:
            handout.append(section_payload(section))

    question_payload = []
    for question in visible_questions:
        item = {
            "id": question.id,
            "ordinal": question.ordinal,
            "stem": question.stem,
            "options": json.loads(question.options_json),
            "is_image_based": question.is_image_based,
            "module_id": question.module_id,
        }
        if reveal:
            item["answer_index"] = question.answer_index
            item["explanation"] = question.explanation
            item["knowledge_point"] = question.knowledge_point
            point_link = db.execute(
                select(QuestionKnowledgePoint).where(
                    QuestionKnowledgePoint.question_id == question.id,
                    QuestionKnowledgePoint.role == "primary",
                )
            ).scalars().first()
            if point_link is not None:
                item["knowledge_point_id"] = point_link.knowledge_point_id
                item["knowledge_point_reliability"] = point_link.reliability
            item["citations"] = [_citation_payload(db, c) for c in by_owner.get(question.id, [])]
        question_payload.append(item)

    responses: dict[str, int | None] = {}
    if draft is not None:
        responses = _attempt_answers(db, draft.id)
    elif submitted is not None:
        responses = _attempt_answers(db, submitted.id)
    if scope_ids is not None:
        allowed = set(scope_ids)
        responses = {qid: idx for qid, idx in responses.items() if qid in allowed}

    result = None
    if reveal and submitted is not None:
        result = _submit_payload(db, pack, submitted, duplicate=False, task_id=task_id)

    parent = db.get(LearningPack, pack.parent_pack_id) if pack.parent_pack_id else None
    children_rows = db.execute(
        select(LearningPack)
        .where(LearningPack.parent_pack_id == pack_id)
        .order_by(LearningPack.created_at)
    ).scalars().all()
    links = db.execute(select(PackSource).where(PackSource.pack_id == pack_id)).scalars().all()
    sources_payload = []
    for link in links:
        source = db.get(SourceMaterial, link.source_id)
        sources_payload.append(
            {
                "source_id": link.source_id,
                "display_name": link.source_name_snapshot,
                "status": source.status if source else "deleted",
                "page_start": link.page_start,
                "page_end": link.page_end,
                "page_count": source.page_count if source else None,
            }
        )

    adoption = db.get(PackPlanVersion, pack_id)
    plan_modules: list[dict] = []
    plan_info: dict = {"linked": False, "assessment": None, "modules": []}
    if adoption is not None:
        version = db.get(LearningPlanVersion, adoption.plan_version_id)
        plan = db.get(LearningPlan, version.plan_id) if version is not None else None
        plan_modules = json.loads(version.modules_json or "[]") if version is not None else []
        plan_info = {
            "linked": True,
            "plan_id": plan.id if plan is not None else None,
            "plan_title": plan.title if plan is not None else None,
            "version_id": version.id if version is not None else None,
            "version_status": version.status if version is not None else None,
            "adopted_at": adoption.adopted_at,
            "delivery": None,
            "assessment": None,
            "modules": plan_modules,
            "items": [
                {
                    "knowledge_point_id": item.get("knowledge_point_id"),
                    "display_name": item.get("display_name"),
                    "suggested_question_count": item.get("suggested_question_count"),
                }
                for item in json.loads(version.items_json or "[]")
            ],
        }
        if version is not None and version.assessment_id:
            brief = assessment_service.snapshot_by_id(db, version.assessment_id)
            if brief is not None:
                planned = {
                    item.get("knowledge_point_id")
                    for item in json.loads(version.items_json or "[]")
                }
                plan_info["assessment"] = {
                    "id": brief.get("id"),
                    "attempt_id": brief.get("attempt_id"),
                    "completed_at": brief.get("completed_at"),
                    "findings": [
                        item for item in brief.get("findings") or []
                        if item.get("knowledge_point_id") in planned
                    ],
                    "route_steps": [
                        item for item in brief.get("route_steps") or []
                        if item.get("knowledge_point_id") in planned
                    ],
                }
        if adoption.delivery_json:
            try:
                plan_info["delivery"] = json.loads(adoption.delivery_json)
            except (TypeError, json.JSONDecodeError):
                plan_info["delivery"] = None

    plan_info["schedule"] = _pack_schedule(db, pack)

    modules_payload = []
    for index, module in enumerate(plan_modules):
        module_id = module.get("module_id")
        module_questions = [
            item for item in question_payload if item.get("module_id") == module_id
        ]
        modules_payload.append(
            {
                **module,
                "title": _clean_module_title(module.get("title"), index),
                "actual_question_count": len(module_questions),
                "handout": [item for item in handout if item.get("module_id") == module_id],
                "questions": module_questions,
                "pages": pages_by_module.get(module_id, []),
                "recap": (recap_by_module.get(module_id) or {}).get("recap"),
            }
        )
    review_payload = {
        "handout": [item for item in handout if not item.get("module_id")],
        "questions": [item for item in question_payload if item.get("module_id") == "review"],
    }
    if scope_payload is not None:
        plan_id = plan_info.get("plan_id")
        scope_payload["calendar_url"] = (
            f"/calendar?plan_id={plan_id}&date={scope_payload['scheduled_date']}"
            if plan_id
            else "/calendar"
        )

    return {
        "id": pack.id,
        "display_title": pack.display_title,
        "created_at": pack.created_at,
        "source_kind": pack.source_kind,
        "difficulty": pack.difficulty,
        "question_count": (
            scope_payload["total"] if scope_payload is not None else pack.question_count
        ),
        "full_question_count": pack.question_count,
        "parent_pack_id": pack.parent_pack_id,
        "parent_label": pack.parent_label,
        "parent_status": ("active" if parent else ("deleted" if pack.parent_pack_id else "none")),
        "plan": plan_info,
        "children": [
            {"id": item.id, "display_title": item.display_title, "created_at": item.created_at}
            for item in children_rows
        ],
        "sources": sources_payload,
        "is_test_double": settings.is_fake,
        "progress": {
            "raw_text": progress.raw_text if progress else "",
            "learned": json.loads(progress.learned_json) if progress else [],
            "unlearned": json.loads(progress.unlearned_json) if progress else [],
            "weak_points": json.loads(progress.weak_points_json) if progress else [],
            "intent": json.loads(progress.intent_json) if progress else [],
            "insufficient": progress.insufficient if progress else False,
        },
        "scope": (handout[0].get("content") if handout else None) or {"covered": [], "not_covered": []},
        "handout": handout,
        "modules": modules_payload,
        "review": review_payload,
        "questions": question_payload,
        "draft": {
            "attempt_id": (draft or submitted).id if (draft or submitted) else None,
            "answers": [{"question_id": qid, "selected_index": idx} for qid, idx in responses.items()],
        },
        "answer_revealed": reveal,
        "result": result,
        "practice_scope": scope_payload,
        "position": _pack_position(db, pack.id),
    }


@router.put("/packs/{pack_id}/draft")
def save_draft(
    pack_id: str,
    payload: DraftRequest,
    task_id: str | None = None,
    db: Session = Depends(get_db),
):
    pack = db.get(LearningPack, pack_id)
    if pack is None:
        raise fail(404, "not_found", "学习计划不存在")
    task = None
    scope_ids = None
    if task_id:
        task, scope_ids = _practice_task(db, pack, task_id)
    if _latest_submitted(db, pack_id, task_id) is not None and _draft(db, pack_id, task_id) is None:
        raise fail(409, "already_submitted", "本轮已提交，如需重做请点击重新作答")
    attempt = _draft(db, pack_id, task_id)
    if attempt is None:
        attempt = Attempt(
            id=new_id("attempt"),
            pack_id=pack_id,
            schedule_task_id=task_id,
            status="draft",
            created_at=now_iso(),
        )
        db.add(attempt)
        db.flush()
    allowed = set(scope_ids) if scope_ids is not None else None
    for item in payload.answers:
        question = db.get(Question, item.question_id)
        if question is None or question.pack_id != pack_id:
            raise fail(422, "question_mismatch", "提交的题目不属于本学习计划", field="answers")
        if allowed is not None and item.question_id not in allowed:
            raise fail(422, "question_not_in_task", "提交的题目不属于本组练习", field="answers")
        row = db.get(AttemptAnswer, (attempt.id, item.question_id))
        if row is None:
            row = AttemptAnswer(attempt_id=attempt.id, question_id=item.question_id)
            db.add(row)
        row.selected_index = item.selected_index
        row.answered_at = now_iso()
    db.commit()
    answers = _attempt_answers(db, attempt.id)
    response = {
        "attempt_id": attempt.id,
        "status": "draft",
        "answered_count": sum(1 for value in answers.values() if value is not None),
    }
    if task is not None:
        response["practice_scope"] = schedule_service.practice_scope_payload(db, pack, task)
    return response


def _submit_payload(
    db: Session,
    pack: LearningPack,
    attempt: Attempt,
    duplicate: bool,
    task_id: str | None = None,
) -> dict:
    all_questions = db.execute(
        select(Question).where(Question.pack_id == pack.id).order_by(Question.ordinal)
    ).scalars().all()
    scope_task = None
    if task_id:
        scope_task = db.get(ScheduleTask, task_id)
    elif attempt.schedule_task_id:
        scope_task = db.get(ScheduleTask, attempt.schedule_task_id)
    scope_ids = (
        schedule_service.ensure_task_question_ids(db, scope_task, pack)
        if scope_task is not None
        else None
    )
    questions = _scoped_questions(all_questions, scope_ids)
    citations = db.execute(
        select(Citation).where(Citation.pack_id == pack.id, Citation.owner_type == "question")
    ).scalars().all()
    by_question: dict[str, list[Citation]] = {}
    for citation in citations:
        by_question.setdefault(citation.owner_id, []).append(citation)
    answers = _attempt_answers(db, attempt.id)
    items = []
    wrong_points: list[str] = []
    for question in questions:
        selected = answers.get(question.id)
        items.append(
            {
                "question_id": question.id,
                "ordinal": question.ordinal,
                "stem": question.stem,
                "options": json.loads(question.options_json),
                "selected_index": selected,
                "answer_index": question.answer_index,
                "is_correct": None if selected is None else selected == question.answer_index,
                "explanation": question.explanation,
                "knowledge_point": question.knowledge_point,
                "module_id": question.module_id,
                "citations": [_citation_payload(db, c) for c in by_question.get(question.id, [])],
            }
        )
        if selected is not None and selected != question.answer_index:
            wrong_points.append(question.knowledge_point)
    scope_payload = None
    if scope_task is not None:
        scope_payload = schedule_service.practice_scope_payload(db, pack, scope_task)
    return {
        "attempt_id": attempt.id,
        "duplicate": duplicate,
        "submitted_at": attempt.submitted_at,
        "score": {
            "correct": attempt.correct_count,
            "wrong": attempt.wrong_count,
            "unanswered": attempt.unanswered_count,
            "total": attempt.total_count,
            "accuracy": round(attempt.correct_count / attempt.total_count, 4) if attempt.total_count else 0.0,
        },
        "questions": items,
        "wrong_knowledge_points": wrong_points,
        "next_round_suggestion": {
            "available": bool(wrong_points),
            "reason": f"本轮有 {len(wrong_points)} 道错题，可转为复习重点" if wrong_points else "本轮无错题",
            "review_focus_preview": wrong_points,
        },
        "assessment": assessment_service.aggregate_for_attempt(db, attempt),
        "practice_scope": scope_payload,
        "is_test_double": settings.is_fake,
    }


@router.post("/packs/{pack_id}/submit")
def submit_pack(pack_id: str, payload: SubmitRequest, db: Session = Depends(get_db)):
    pack = db.get(LearningPack, pack_id)
    if pack is None:
        raise fail(404, "not_found", "学习计划不存在")
    task = None
    scope_ids = None
    if payload.task_id:
        task, scope_ids = _practice_task(db, pack, payload.task_id)
    same_query = select(Attempt).where(
        Attempt.pack_id == pack_id,
        Attempt.status == "submitted",
        Attempt.idempotency_key == payload.idempotency_key,
    )
    if payload.task_id:
        same_query = same_query.where(Attempt.schedule_task_id == payload.task_id)
    else:
        same_query = same_query.where(Attempt.schedule_task_id.is_(None))
    same = db.execute(same_query).scalars().first()
    if same is not None:
        return _submit_payload(
            db, pack, same, duplicate=True, task_id=payload.task_id
        )

    attempt = _draft(db, pack_id, payload.task_id)
    if attempt is None and _latest_submitted(db, pack_id, payload.task_id) is not None:
        raise fail(409, "already_submitted", "本轮已提交，如需重做请点击重新作答")
    if attempt is None:
        attempt = Attempt(
            id=new_id("attempt"),
            pack_id=pack_id,
            schedule_task_id=payload.task_id,
            status="draft",
            created_at=now_iso(),
        )
        db.add(attempt)
        db.flush()
    allowed = set(scope_ids) if scope_ids is not None else None
    for item in payload.answers:
        question = db.get(Question, item.question_id)
        if question is None or question.pack_id != pack_id:
            raise fail(422, "question_mismatch", "提交的题目不属于本学习计划", field="answers")
        if allowed is not None and item.question_id not in allowed:
            raise fail(422, "question_not_in_task", "提交的题目不属于本组练习", field="answers")
        row = db.get(AttemptAnswer, (attempt.id, item.question_id))
        if row is None:
            row = AttemptAnswer(attempt_id=attempt.id, question_id=item.question_id)
            db.add(row)
        row.selected_index = item.selected_index
        row.answered_at = now_iso()
    db.flush()

    all_questions = db.execute(
        select(Question).where(Question.pack_id == pack_id).order_by(Question.ordinal)
    ).scalars().all()
    questions = _scoped_questions(all_questions, scope_ids) if task is not None else all_questions
    question_rows = [{"id": q.id, "answer_index": q.answer_index} for q in questions]
    existing = _attempt_answers(db, attempt.id)
    graded = grade(
        question_rows,
        [
            {"question_id": qid, "selected_index": idx}
            for qid, idx in existing.items()
            if allowed is None or qid in allowed
        ],
    )
    attempt.status = "submitted"
    attempt.idempotency_key = payload.idempotency_key
    attempt.correct_count = graded.correct
    attempt.wrong_count = graded.wrong
    attempt.unanswered_count = graded.unanswered
    attempt.total_count = graded.total
    attempt.submitted_at = now_iso()
    for item in graded.answers:
        row = db.get(AttemptAnswer, (attempt.id, item.question_id))
        if row is not None:
            row.is_correct = item.is_correct
    wrong_ids = [item.question_id for item in graded.answers if item.is_correct is False]
    wrong_questions = [q for q in questions if q.id in wrong_ids]
    create_review_focus(db, pack_id, attempt.id, wrong_questions)
    evidence_service.record_attempt_evidence(db, pack, attempt, graded)
    schedule_service.settle_practice_tasks(db, pack, attempt, payload.task_id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        same_query = select(Attempt).where(
            Attempt.pack_id == pack_id,
            Attempt.status == "submitted",
            Attempt.idempotency_key == payload.idempotency_key,
        )
        if payload.task_id:
            same_query = same_query.where(Attempt.schedule_task_id == payload.task_id)
        else:
            same_query = same_query.where(Attempt.schedule_task_id.is_(None))
        same = db.execute(same_query).scalars().first()
        if same is not None:
            return _submit_payload(
                db, pack, same, duplicate=True, task_id=payload.task_id
            )
        raise fail(409, "already_submitted", "本轮已提交，如需重做请点击重新作答")
    return _submit_payload(
        db, pack, attempt, duplicate=False, task_id=payload.task_id
    )


@router.post("/packs/{pack_id}/assessment")
def run_pack_assessment(pack_id: str, payload: AssessmentRequest, db: Session = Depends(get_db)):
    pack = db.get(LearningPack, pack_id)
    if pack is None or pack.source_kind == "diagnostic":
        raise fail(404, "not_found", "学习计划不存在")
    attempt = db.get(Attempt, payload.attempt_id)
    if attempt is None or attempt.pack_id != pack_id:
        raise fail(404, "not_found", "答卷不存在")
    if attempt.status != "submitted":
        raise fail(409, "assessment_attempt_not_submitted", "只有已交卷的练习才能生成个人档案")
    try:
        return assessment_service.generate_for_attempt(
            db, attempt, force=payload.force, source_id=payload.source_id
        )
    except assessment_service.AssessmentError as exc:
        raise fail(exc.status, exc.code, exc.message)


@router.get("/packs/{pack_id}/assessment")
def get_pack_assessment(
    pack_id: str, attempt_id: str | None = None, db: Session = Depends(get_db)
):
    pack = db.get(LearningPack, pack_id)
    if pack is None or pack.source_kind == "diagnostic":
        raise fail(404, "not_found", "学习计划不存在")
    attempt = None
    if attempt_id:
        attempt = db.get(Attempt, attempt_id)
        if attempt is None or attempt.pack_id != pack_id:
            raise fail(404, "not_found", "答卷不存在")
    else:
        attempt = _latest_submitted(db, pack_id)
    snapshot = (
        assessment_service.aggregate_for_attempt(db, attempt)
        if attempt is not None
        else None
    )
    return {
        "available": snapshot is not None,
        "snapshot": snapshot,
    }


@router.get("/learner-profile")
def learner_profile(source_id: str | None = None, db: Session = Depends(get_db)):
    if source_id is not None:
        source = db.get(SourceMaterial, source_id)
        if source is None or source.status != "active":
            raise fail(410, "source_not_found", "资料已删除或不可用")
    return assessment_service.profile_payload(db, source_id)


@router.post("/packs/{pack_id}/reattempt", status_code=201)
def reattempt(
    pack_id: str, task_id: str | None = None, db: Session = Depends(get_db)
):
    pack = db.get(LearningPack, pack_id)
    if pack is None:
        raise fail(404, "not_found", "学习计划不存在")
    task = None
    scope_ids = None
    if task_id:
        task, scope_ids = _practice_task(db, pack, task_id)
    submitted = _latest_submitted(db, pack_id, task_id)
    if submitted is None:
        raise fail(409, "not_submitted", "请先提交本轮作答")
    existing = _draft(db, pack_id, task_id)
    if existing is not None:
        return {"attempt_id": existing.id, "status": "draft"}
    attempt = Attempt(
        id=new_id("attempt"),
        pack_id=pack_id,
        schedule_task_id=task_id,
        status="draft",
        created_at=now_iso(),
    )
    db.add(attempt)
    db.flush()
    if task is not None:
        allowed = set(scope_ids or [])
        previous = _attempt_answers(db, submitted.id)
        for question_id in allowed:
            if question_id not in previous:
                continue
            row = db.get(AttemptAnswer, (attempt.id, question_id))
            if row is None:
                row = AttemptAnswer(attempt_id=attempt.id, question_id=question_id)
                db.add(row)
            row.selected_index = previous[question_id]
            row.answered_at = now_iso()
    db.commit()
    return {"attempt_id": attempt.id, "status": "draft"}


@router.get("/packs/{pack_id}/review-focus")
def list_review_focus(pack_id: str, db: Session = Depends(get_db)):
    if db.get(LearningPack, pack_id) is None:
        raise fail(404, "not_found", "学习计划不存在")
    submitted = _latest_submitted(db, pack_id)
    if submitted is None:
        return {
            "available": False,
            "reason": "请先提交本轮作答",
            "items": [],
            "attempt_id": None,
        }
    rows = db.execute(
        select(ReviewFocus)
        .where(
            ReviewFocus.source_pack_id == pack_id,
            ReviewFocus.source_attempt_id == submitted.id,
            ReviewFocus.is_deleted.is_(False),
        )
        .order_by(ReviewFocus.ordinal)
    ).scalars().all()
    merged: list[dict] = []
    seen: dict[str, dict] = {}
    for row in rows:
        key = row.knowledge_point.strip()
        if key in seen:
            if row.suggestion and row.suggestion not in (seen[key].get("suggestion") or ""):
                seen[key]["suggestion"] = (seen[key].get("suggestion") or "") + "；" + row.suggestion
            continue
        item = {
            "id": row.id,
            "knowledge_point": row.knowledge_point,
            "knowledge_point_id": row.knowledge_point_id,
            "suggestion": row.suggestion,
            "edited_text": row.edited_text,
            "question_id": row.question_id,
        }
        seen[key] = item
        merged.append(item)
    return {
        "available": bool(merged),
        "reason": "本轮存在错题" if merged else "本轮无错题，可修改进度后再练",
        "items": merged,
        "attempt_id": submitted.id,
    }


@router.put("/packs/{pack_id}/review-focus/{focus_id}")
def update_review_focus(pack_id: str, focus_id: str, payload: ReviewFocusText, db: Session = Depends(get_db)):
    row = db.get(ReviewFocus, focus_id)
    if row is None or row.source_pack_id != pack_id or row.is_deleted:
        raise fail(404, "not_found", "复习重点不存在")
    text = payload.text.strip()
    if not text:
        raise fail(422, "focus_text_required", "复习重点不能为空", field="text")
    row.edited_text = text
    db.commit()
    return {"id": row.id, "edited_text": row.edited_text}


@router.delete("/packs/{pack_id}/review-focus/{focus_id}")
def remove_review_focus(pack_id: str, focus_id: str, db: Session = Depends(get_db)):
    row = db.get(ReviewFocus, focus_id)
    if row is None or row.source_pack_id != pack_id or row.is_deleted:
        raise fail(404, "not_found", "复习重点不存在")
    row.is_deleted = True
    db.commit()
    return {"deleted": True, "id": row.id}


@router.post("/packs/{pack_id}/review-focus", status_code=201)
def add_review_focus(pack_id: str, payload: ReviewFocusItem, db: Session = Depends(get_db)):
    if db.get(LearningPack, pack_id) is None:
        raise fail(404, "not_found", "学习计划不存在")
    submitted = _latest_submitted(db, pack_id)
    if submitted is None:
        raise fail(409, "not_submitted", "请先提交本轮作答")
    text = payload.knowledge_point.strip()
    if not text:
        raise fail(422, "focus_text_required", "复习重点不能为空", field="knowledge_point")
    rows = db.execute(
        select(ReviewFocus).where(
            ReviewFocus.source_pack_id == pack_id,
            ReviewFocus.source_attempt_id == submitted.id,
            ReviewFocus.is_deleted.is_(False),
        )
    ).scalars().all()
    if len(rows) >= 5:
        raise fail(422, "review_focus_limit", "复习重点最多 5 条，请先合并")
    row = ReviewFocus(
        id=new_id("review"),
        source_pack_id=pack_id,
        source_attempt_id=submitted.id,
        question_id="",
        knowledge_point=text,
        suggestion=payload.suggestion or "",
        edited_text=None,
        ordinal=max([item.ordinal for item in rows] or [0]) + 1,
        is_deleted=False,
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "knowledge_point": row.knowledge_point, "suggestion": row.suggestion}


def _point_payload(point: KnowledgePoint) -> dict:
    return {
        "id": point.id,
        "source_id": point.source_id,
        "display_name": point.display_name,
        "status": point.status,
        "created_at": point.created_at,
        "updated_at": point.updated_at,
    }


@router.get("/knowledge-points")
def list_knowledge_points(source_id: str | None = None, db: Session = Depends(get_db)):
    query = select(KnowledgePoint)
    if source_id:
        query = query.where(KnowledgePoint.source_id == source_id)
    rows = db.execute(query.order_by(KnowledgePoint.display_name, KnowledgePoint.id)).scalars().all()
    return {"items": [_point_payload(point) for point in rows]}


@router.put("/knowledge-points/{point_id}")
def rename_point(point_id: str, payload: KnowledgePointRename, db: Session = Depends(get_db)):
    point = db.get(KnowledgePoint, point_id)
    if point is None:
        raise fail(404, "not_found", "考点不存在")
    messages = {
        "display_name_required": "考点名称不能为空",
        "knowledge_point_too_generic": "泛称只能作为章节分类，不能作为掌握情况的评估对象",
        "knowledge_point_duplicate": "同一份资料里已有同名考点",
    }
    try:
        rename_knowledge_point(db, point, payload.display_name)
        db.commit()
    except ValueError as exc:
        code = str(exc)
        raise fail(422, code, messages.get(code, "考点名称不合法"), field="display_name")
    return _point_payload(point)


@router.get("/knowledge-points/{point_id}/evidence")
def knowledge_point_evidence(point_id: str, db: Session = Depends(get_db)):
    point = db.get(KnowledgePoint, point_id)
    if point is None:
        raise fail(404, "not_found", "考点不存在")
    return evidence_service.summary_for_point(db, point)


@router.get("/packs/{pack_id}/evidence")
def pack_evidence(pack_id: str, db: Session = Depends(get_db)):
    pack = db.get(LearningPack, pack_id)
    if pack is None or pack.source_kind == "diagnostic":
        raise fail(404, "not_found", "学习计划不存在")
    return evidence_service.pack_evidence(db, pack_id)


_DIAGNOSIS_CONFLICT_CODES = {
    "session_closed",
    "session_confirmed",
    "question_superseded",
    "diagnostic_confirm_conflict",
}
_DIAGNOSIS_NOT_FOUND_CODES = {
    "diagnostic_session_not_found",
    "question_not_found",
    "answer_not_found",
    "round_not_found",
}
_DIAGNOSIS_PARAM_ALIASES = {
    "invalid_option": "diagnostic_answer_invalid",
    "invalid_status": "diagnostic_answer_invalid",
    "diagnostic_question_mismatch": "diagnostic_answer_invalid",
}


def _diagnosis_error(exc: diagnosis.DiagnosisError) -> HTTPException:
    code = _DIAGNOSIS_PARAM_ALIASES.get(exc.code, exc.code)
    if code == "internal_error":
        status = 500
    elif code in _DIAGNOSIS_CONFLICT_CODES:
        status = 409
    elif code in _DIAGNOSIS_NOT_FOUND_CODES:
        status = 404
    else:
        status = 422
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": exc.message, "actions": []}},
    )


def _get_diagnosis_session(db: Session, session_id: str) -> DiagnosticSession:
    session = db.get(DiagnosticSession, session_id)
    if session is None:
        raise fail(404, "not_found", "诊断会话不存在")
    return session



@router.post("/sources/{source_id}/diagnosis/sessions", status_code=201)
def start_diagnosis(
    source_id: str,
    payload: DiagnosisStartRequest,
    db: Session = Depends(get_db),
):
    source = db.get(SourceMaterial, source_id)
    if source is None or source.status != "active" and not payload.knowledge_base_id:
        raise fail(410, "source_not_found", "资料已删除或不可用，无法开始诊断")
    source_scope = []
    knowledge_base_id = payload.knowledge_base_id
    if knowledge_base_id:
        try:
            specs = kb_service.scope_specs(
                db,
                knowledge_base_id,
                payload.scope_mode,
                payload.source_ids,
                payload.page_ranges,
            )
        except Exception as exc:  # noqa: BLE001
            raise _kb_error(exc)
        source_scope = [
            {
                "source_id": spec["source_id"],
                "source_name": spec.get("source_name"),
                "page_start": spec.get("page_start"),
                "page_end": spec.get("page_end"),
            }
            for spec in specs
        ]
        source = db.get(SourceMaterial, specs[0]["source_id"])
    try:
        result = diagnosis.start_session(
            db,
            source,
            goal=payload.goal,
            question_count=payload.question_count,
            learning_mode=payload.learning_mode,
            knowledge_base_id=knowledge_base_id,
            source_scope=source_scope,
            session_key=payload.idempotency_key,
            direct_view=payload.direct_view,
        )
    except diagnosis.DiagnosisError as exc:
        raise _diagnosis_error(exc)
    if payload.direct_view:
        session = db.get(DiagnosticSession, result["session"]["id"])
        preview = diagnosis.build_plan_preview(
            db,
            session,
            goal=payload.goal,
            question_count=payload.question_count,
            learning_mode=payload.learning_mode,
        )
        result = {
            **result,
            "session": preview["session"],
            "plan": preview["plan"],
            "version": preview["version"],
            "diagnostic_basis": preview["diagnostic_basis"],
            "missing": preview["missing"],
        }
    return result


@router.get("/diagnosis/sessions/{session_id}")
def get_diagnosis_session(session_id: str, db: Session = Depends(get_db)):
    return diagnosis.session_payload(db, _get_diagnosis_session(db, session_id))


@router.get("/diagnosis/sessions/{session_id}/transcript")
def get_diagnosis_transcript(session_id: str, db: Session = Depends(get_db)):
    return diagnosis.expand_session(db, _get_diagnosis_session(db, session_id))


@router.post("/diagnosis/sessions/{session_id}/answers")
def answer_diagnosis(
    session_id: str,
    payload: DiagnosisAnswerRequest,
    db: Session = Depends(get_db),
):
    session = _get_diagnosis_session(db, session_id)
    try:
        return diagnosis.submit_answer(
            db,
            session,
            payload.question_id,
            selected_index=payload.selected_index,
            status=payload.status,
            answer_key=payload.idempotency_key,
        )
    except diagnosis.DiagnosisError as exc:
        raise _diagnosis_error(exc)


@router.post("/diagnosis/sessions/{session_id}/answers/{answer_id}/reopen")
def reopen_diagnosis_answer(
    session_id: str,
    answer_id: str,
    db: Session = Depends(get_db),
):
    session = _get_diagnosis_session(db, session_id)
    try:
        return diagnosis.reopen_answer(db, session, answer_id)
    except diagnosis.DiagnosisError as exc:
        raise _diagnosis_error(exc)


@router.post("/diagnosis/sessions/{session_id}/finish")
def finish_diagnosis(
    session_id: str,
    payload: DiagnosisPlanRequest,
    db: Session = Depends(get_db),
):
    session = _get_diagnosis_session(db, session_id)
    try:
        return diagnosis.build_plan_preview(
            db,
            session,
            goal=payload.goal,
            question_count=payload.question_count,
            learning_mode=payload.learning_mode,
            schedule_options=_schedule_options(payload),
        )
    except diagnosis.DiagnosisError as exc:
        raise _diagnosis_error(exc)


@router.post("/diagnosis/sessions/{session_id}/plan")
def preview_diagnosis_plan(
    session_id: str,
    payload: DiagnosisPlanRequest,
    db: Session = Depends(get_db),
):
    session = _get_diagnosis_session(db, session_id)
    try:
        return diagnosis.build_plan_preview(
            db,
            session,
            goal=payload.goal,
            question_count=payload.question_count,
            learning_mode=payload.learning_mode,
            schedule_options=_schedule_options(payload),
        )
    except diagnosis.DiagnosisError as exc:
        raise _diagnosis_error(exc)


@router.post("/diagnosis/sessions/{session_id}/replan")
def replan_diagnosis(
    session_id: str,
    payload: DiagnosisPlanRequest,
    db: Session = Depends(get_db),
):
    session = _get_diagnosis_session(db, session_id)
    try:
        return diagnosis.replan_session(
            db,
            session,
            goal=payload.goal,
            learning_mode=payload.learning_mode,
        )
    except diagnosis.DiagnosisError as exc:
        raise _diagnosis_error(exc)


@router.post("/diagnosis/sessions/{session_id}/confirm")
def confirm_diagnosis(
    session_id: str,
    payload: DiagnosisConfirmRequest,
    db: Session = Depends(get_db),
):
    session = _get_diagnosis_session(db, session_id)
    source = db.get(SourceMaterial, session.source_id)
    if source is None:
        raise fail(404, "not_found", "资料不存在")
    try:
        result = diagnosis.confirm_session(
            db, session, source, _schedule_options(payload)
        )
    except diagnosis.DiagnosisError as exc:
        raise _diagnosis_error(exc)
    plan = None
    if session.plan_version_id:
        version = db.get(LearningPlanVersion, session.plan_version_id)
        if version is not None:
            plan = plan_service.plan_payload(db, db.get(LearningPlan, version.plan_id))
    return {
        "session": diagnosis.session_payload(db, session),
        "pack_id": result.get("pack_id"),
        "job_id": result.get("job_id"),
        "job_status": result.get("job_status"),
        "container_pack_id": result.get("container_pack_id"),
        "duplicate": result.get("duplicate"),
        "retry_available": result.get("retry_available", False),
        "actions": result.get("actions", []),
        "plan_id": result.get("plan_id"),
        "error": result.get("error"),
        "plan": plan,
    }


@router.post("/diagnosis/sessions/{session_id}/retry")
def retry_diagnosis(session_id: str, db: Session = Depends(get_db)):
    session = _get_diagnosis_session(db, session_id)
    try:
        return diagnosis.retry_turn(db, session)
    except diagnosis.DiagnosisError as exc:
        raise _diagnosis_error(exc)


@router.post("/diagnosis/sessions/{session_id}/abandon")
def abandon_diagnosis(session_id: str, db: Session = Depends(get_db)):
    session = _get_diagnosis_session(db, session_id)
    if session.status != "confirmed":
        session.status = "abandoned"
        session.updated_at = now_iso()
        db.commit()
    return diagnosis.session_payload(db, session)


@router.post("/plans", status_code=201)
def create_learning_plan(payload: PlanCreateRequest, db: Session = Depends(get_db)):
    return plan_service.create_plan(db, payload)


@router.get("/plans")
def list_plans(limit: int = 50, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 200))
    rows = db.execute(
        select(LearningPlan).order_by(LearningPlan.updated_at.desc()).limit(limit)
    ).scalars().all()
    return {"items": [plan_service.plan_payload(db, row) for row in rows]}


@router.get("/plans/{plan_id}")
def get_learning_plan(plan_id: str, db: Session = Depends(get_db)):
    plan = db.get(LearningPlan, plan_id)
    if plan is None:
        raise fail(404, "not_found", "学习计划不存在")
    return plan_service.plan_payload(db, plan)


@router.post("/plans/{plan_id}/versions", status_code=201)
def add_plan_version(plan_id: str, payload: PlanVersionRequest, db: Session = Depends(get_db)):
    plan = db.get(LearningPlan, plan_id)
    if plan is None:
        raise fail(404, "not_found", "学习计划不存在")
    return plan_service.add_version(db, plan, payload)


@router.post("/plans/{plan_id}/versions/{version_id}/confirm")
def confirm_plan_version(plan_id: str, version_id: str, db: Session = Depends(get_db)):
    plan = db.get(LearningPlan, plan_id)
    if plan is None:
        raise fail(404, "not_found", "学习计划不存在")
    version = db.get(LearningPlanVersion, version_id)
    if version is None or version.plan_id != plan_id:
        raise fail(404, "not_found", "计划版本不存在")
    if version.status == "confirmed":
        return plan_service.plan_payload(db, plan)
    missing = plan_service.confirmation_missing(db, version)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "plan_incomplete",
                    "message": "计划缺少必要信息",
                    "missing": missing,
                    "actions": [],
                }
            },
        )
    problems = plan_service.validate_allocation(version)
    if problems:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "plan_allocation_invalid",
                    "message": "计划题量分配不合法",
                    "problems": problems,
                    "actions": [],
                }
            },
        )
    return plan_service.confirm_version(db, plan, version)


@router.post("/schedule/preview")
def preview_schedule(
    payload: SchedulePreviewRequest,
    db: Session = Depends(get_db),
):
    version = db.get(LearningPlanVersion, payload.plan_version_id)
    if version is None:
        raise fail(404, "plan_version_not_found", "学习计划版本不存在")
    try:
        return schedule_service.preview_schedule(version, _schedule_options(payload))
    except schedule_service.ScheduleError as exc:
        raise _schedule_http_error(exc)


@router.post("/plans/{plan_id}/versions/{version_id}/schedule")
def save_plan_schedule(
    plan_id: str,
    version_id: str,
    payload: PlanScheduleRequest,
    db: Session = Depends(get_db),
):
    plan = db.get(LearningPlan, plan_id)
    if plan is None:
        raise fail(404, "not_found", "学习计划不存在")
    version = db.get(LearningPlanVersion, version_id)
    if version is None or version.plan_id != plan_id:
        raise fail(404, "not_found", "学习计划版本不存在")
    if version.status != "confirmed":
        raise fail(409, "plan_version_not_confirmed", "学习计划确认后才能安排日程")
    try:
        result, duplicate = schedule_service.ensure_schedule(
            db, plan, version, _schedule_options(payload)
        )
        db.commit()
    except schedule_service.ScheduleError as exc:
        db.rollback()
        raise _schedule_http_error(exc)
    return {**result, "duplicate": duplicate}


@router.post("/schedule/tasks/{task_id}/complete")
def complete_schedule_task(task_id: str, db: Session = Depends(get_db)):
    task = db.get(ScheduleTask, task_id)
    if task is None:
        raise fail(404, "schedule_task_not_found", "日程任务不存在")
    try:
        result = schedule_service.mark_task_complete(db, task)
        db.commit()
    except schedule_service.ScheduleError as exc:
        db.rollback()
        raise _schedule_http_error(exc)
    tasks = schedule_service.tasks_for_version(db, task.plan_version_id)
    return {
        **result,
        "version_id": task.plan_version_id,
        "progress": schedule_service.progress_payload(tasks),
        "today": schedule_service.today_local().isoformat(),
    }


@router.get("/schedule")
def get_schedule(
    plan_id: str | None = None,
    version_id: str | None = None,
    month: str | None = None,
    date: str | None = None,
    db: Session = Depends(get_db),
):
    today = schedule_service.today_local()
    try:
        selected = schedule_service.parse_iso_date(date, "date") or today
    except schedule_service.ScheduleError as exc:
        raise _schedule_http_error(exc)
    month_start, month_end = _month_bounds(month, selected)

    plans = []
    rows = db.execute(
        select(LearningPlan).order_by(
            LearningPlan.updated_at.desc(), LearningPlan.created_at.desc()
        )
    ).scalars().all()
    for plan in rows:
        version = schedule_service.active_version(db, plan)
        if version is None:
            continue
        tasks = schedule_service.tasks_for_version(db, version.id)
        pack = schedule_service.latest_pack_for_version(db, version.id)
        config = schedule_service.schedule_config(version)
        progress = schedule_service.progress_payload(tasks, today)
        plans.append(
            {
                "id": plan.id,
                "title": plan.title,
                "display_title": _plan_display_title(plan, pack),
                "version_id": version.id,
                "unassigned": not bool(tasks),
                "pack_id": pack.id if pack is not None else None,
                "pack_title": pack.display_title if pack is not None else None,
                "progress": progress,
                "estimated_end_date": config.get("planned_end_date"),
                "target_date": config.get("target_date"),
                "updated_at": plan.updated_at,
            }
        )

    chosen = next((item for item in plans if item["id"] == plan_id), None)
    if chosen is None:
        recent = _recent_pack(db)
        if recent is not None:
            adoption = db.get(PackPlanVersion, recent.id)
            if adoption is not None:
                recent_version = db.get(LearningPlanVersion, adoption.plan_version_id)
                if recent_version is not None:
                    chosen = next(
                        (item for item in plans if item["id"] == recent_version.plan_id),
                        None,
                    )
    if chosen is None and plans:
        chosen = plans[0]

    plan = db.get(LearningPlan, chosen["id"]) if chosen is not None else None
    version = None
    if plan is not None:
        version = db.get(LearningPlanVersion, chosen["version_id"])
        if version_id:
            candidate = db.get(LearningPlanVersion, version_id)
            if (
                candidate is not None
                and candidate.plan_id == plan.id
                and candidate.status == "confirmed"
            ):
                version = candidate

    tasks = schedule_service.tasks_for_version(db, version.id) if version is not None else []
    pack = (
        schedule_service.latest_pack_for_version(db, version.id)
        if version is not None
        else None
    )
    config = schedule_service.schedule_config(version) if version is not None else {}
    progress = schedule_service.progress_payload(tasks, today)
    base_pack = pack
    if base_pack is not None and version is not None:
        schedule_service.bind_practice_questions(db, base_pack, version.id)
    serialized = []
    for task in tasks:
        task_pack = (
            schedule_service.pack_for_task(db, task, base_pack)
            if base_pack is not None
            else None
        )
        if task.task_type == "practice" and task_pack is not None:
            schedule_service.ensure_task_question_ids(db, task, task_pack)
        serialized.append(schedule_service.serialize_task(task, today, pack=task_pack))
    page_index: dict[str, list[dict]] = {}
    if base_pack is not None:
        page_index = _pack_page_index(db, base_pack.id)
        _attach_task_pages(serialized, page_index)
    selected_tasks = [
        task for task in serialized if task["scheduled_date"] == selected.isoformat()
    ]
    today_tasks = [
        task for task in serialized if task["scheduled_date"] == today.isoformat()
    ]
    month_days: dict[str, dict] = {}
    for task in serialized:
        task_date = task["scheduled_date"]
        if task_date < month_start.isoformat() or task_date > month_end.isoformat():
            continue
        entry = month_days.setdefault(
            task_date,
            {
                "date": task_date,
                "total": 0,
                "completed": 0,
                "overdue": 0,
                "estimated_minutes": 0,
            },
        )
        entry["total"] += 1
        entry["estimated_minutes"] += int(task["estimated_minutes"] or 0)
        if task["status"] == "completed":
            entry["completed"] += 1
        elif task_date < today.isoformat():
            entry["overdue"] += 1

    pack_status = "ready" if pack is not None else ("pending" if version is not None else "missing")
    next_task = schedule_service.next_learnable_task(serialized)
    today_all_done = bool(today_tasks) and all(
        task["status"] == "completed" for task in today_tasks
    )
    shift = (
        schedule_service.shift_preview(db, version, today)
        if version is not None and tasks
        else {"available": False, "dismissed": False}
    )
    brief = (
        brief_service.build_brief(
            db,
            plan,
            version,
            selected,
            tasks=serialized,
            progress=progress,
            next_task=next_task,
            today=today,
            page_index=page_index,
        )
        if version is not None
        else None
    )
    return {
        "today": today.isoformat(),
        "selected_date": selected.isoformat(),
        "month": month_start.strftime("%Y-%m"),
        "month_start": month_start.isoformat(),
        "month_end": month_end.isoformat(),
        "plans": plans,
        "plan": (
            {
                "id": plan.id,
                "title": plan.title,
                "display_title": _plan_display_title(plan, pack),
                "version_id": version.id,
                "unassigned": not bool(tasks),
                "pack_id": pack.id if pack is not None else None,
                "pack_title": pack.display_title if pack is not None else None,
                "pack_url": f"/packs/{pack.id}" if pack is not None else None,
                "pack_status": pack_status,
                "session_id": version.diagnostic_session_id,
            }
            if plan is not None
            else None
        ),
        "schedule": config,
        "progress": progress,
        "month_days": month_days,
        "selected_tasks": selected_tasks,
        "selected_estimated_minutes": sum(
            int(task["estimated_minutes"] or 0) for task in selected_tasks
        ),
        "today_tasks": today_tasks,
        "today_all_done": today_all_done,
        "next_task": next_task,
        "shift": shift,
        "brief": brief,
        "overdue_count": progress["overdue_count"],
    }


@router.post("/schedule/shift/preview")
def preview_schedule_shift(payload: ScheduleShiftRequest, db: Session = Depends(get_db)):
    plan, version = _schedule_version(db, payload.plan_id, payload.version_id)
    try:
        preview = schedule_service.shift_preview(db, version)
    except schedule_service.ScheduleError as exc:
        raise _schedule_http_error(exc)
    return {
        "plan_id": plan.id,
        "version_id": version.id,
        "today": schedule_service.today_local().isoformat(),
        "preview": preview,
    }


@router.post("/schedule/shift/confirm")
def confirm_schedule_shift(payload: ScheduleShiftRequest, db: Session = Depends(get_db)):
    plan, version = _schedule_version(db, payload.plan_id, payload.version_id)
    try:
        result = schedule_service.apply_shift(db, plan, version, payload.preview_key or "")
        tasks = schedule_service.tasks_for_version(db, version.id)
        result["progress"] = schedule_service.progress_payload(tasks)
        result["today"] = schedule_service.today_local().isoformat()
        result["shift"] = schedule_service.shift_preview(db, version)
        db.commit()
    except schedule_service.ScheduleError as exc:
        db.rollback()
        raise _schedule_http_error(exc)
    return result


@router.post("/schedule/shift/dismiss")
def dismiss_schedule_shift(payload: ScheduleShiftRequest, db: Session = Depends(get_db)):
    _, version = _schedule_version(db, payload.plan_id, payload.version_id)
    try:
        result = schedule_service.dismiss_shift(db, version, payload.preview_key or "")
        db.commit()
    except schedule_service.ScheduleError as exc:
        db.rollback()
        raise _schedule_http_error(exc)
    return result


@router.post("/schedule/tasks/{task_id}/reschedule/preview")
def preview_task_reschedule(
    task_id: str,
    payload: ScheduleReschedulePreviewRequest,
    db: Session = Depends(get_db),
):
    task = db.get(ScheduleTask, task_id)
    if task is None:
        raise fail(404, "schedule_task_not_found", "日程任务不存在")
    version = db.get(LearningPlanVersion, task.plan_version_id)
    if version is None:
        raise fail(404, "plan_version_not_found", "学习计划版本不存在")
    try:
        preview = schedule_service.reschedule_preview(
            db, version, task, payload.scheduled_date
        )
    except schedule_service.ScheduleError as exc:
        raise _schedule_http_error(exc)
    return {"version_id": version.id, "preview": preview}


@router.post("/schedule/tasks/{task_id}/reschedule")
def reschedule_task(
    task_id: str,
    payload: ScheduleRescheduleRequest,
    db: Session = Depends(get_db),
):
    task = db.get(ScheduleTask, task_id)
    if task is None:
        raise fail(404, "schedule_task_not_found", "日程任务不存在")
    version = db.get(LearningPlanVersion, task.plan_version_id)
    if version is None:
        raise fail(404, "plan_version_not_found", "学习计划版本不存在")
    try:
        result = schedule_service.reschedule_task(
            db,
            version,
            task,
            payload.scheduled_date,
            payload.expected_date,
        )
        tasks = schedule_service.tasks_for_version(db, version.id)
        result["progress"] = schedule_service.progress_payload(tasks)
        result["schedule"] = schedule_service.schedule_config(version)
        db.commit()
    except schedule_service.ScheduleError as exc:
        db.rollback()
        raise _schedule_http_error(exc)
    return result


@router.get("/schedule/brief")
def get_schedule_brief(
    plan_id: str | None = None,
    version_id: str | None = None,
    date: str | None = None,
    db: Session = Depends(get_db),
):
    plan, version = _schedule_version(db, plan_id, version_id)
    today = schedule_service.today_local()
    try:
        target = schedule_service.parse_iso_date(date, "date") or today
    except schedule_service.ScheduleError as exc:
        raise _schedule_http_error(exc)
    tasks = schedule_service.tasks_for_version(db, version.id)
    pack = schedule_service.latest_pack_for_version(db, version.id)
    if pack is not None:
        schedule_service.bind_practice_questions(db, pack, version.id)
    serialized = []
    for task in tasks:
        task_pack = schedule_service.pack_for_task(db, task, pack) if pack is not None else None
        if task.task_type == "practice" and task_pack is not None:
            schedule_service.ensure_task_question_ids(db, task, task_pack)
        serialized.append(schedule_service.serialize_task(task, today, pack=task_pack))
    page_index: dict[str, list[dict]] = {}
    if pack is not None:
        page_index = _pack_page_index(db, pack.id)
        _attach_task_pages(serialized, page_index)
    progress = schedule_service.progress_payload(tasks, today)
    return brief_service.build_brief(
        db,
        plan,
        version,
        target,
        tasks=serialized,
        progress=progress,
        next_task=schedule_service.next_learnable_task(serialized),
        today=today,
        page_index=page_index,
    )


@router.get("/knowledge-points/{point_id}/mastery")
def point_mastery(point_id: str, db: Session = Depends(get_db)):
    point = db.get(KnowledgePoint, point_id)
    if point is None:
        raise fail(404, "not_found", "考点不存在")
    result = mastery.evaluate_point_mastery(db, point)
    return {"knowledge_point": _point_payload(point), "mastery": result.to_dict()}


@router.get("/packs/{pack_id}/mastery-changes")
def pack_mastery_changes(
    pack_id: str,
    attempt_id: str | None = None,
    db: Session = Depends(get_db),
):
    pack = db.get(LearningPack, pack_id)
    if pack is None or pack.source_kind == "diagnostic":
        raise fail(404, "not_found", "学习计划不存在")
    if not attempt_id:
        latest = _latest_submitted(db, pack_id)
        attempt_id = latest.id if latest is not None else None
    if not attempt_id:
        return {"pack_id": pack_id, "changes": [], "unanswered_points": [], "reason": "尚未提交"}
    return mastery.pack_mastery_changes(db, pack_id, attempt_id)


@router.get("/sources/{source_id}/learning-context")
def learning_context(source_id: str, db: Session = Depends(get_db)):
    source = db.get(SourceMaterial, source_id)
    if source is None:
        raise fail(404, "not_found", "资料不存在")
    points = db.execute(
        select(KnowledgePoint).where(KnowledgePoint.source_id == source_id)
    ).scalars().all()
    previous = plan_service.latest_confirmed_version(db, source_id)
    scope = json.loads(previous.scope_json or "{}") if previous is not None else {}
    direct_eligible = previous is not None and bool((scope.get("goal") or "").strip())
    return {
        "source": {
            "id": source.id,
            "display_name": source.display_name,
            "status": source.status,
        },
        "can_continue": source.status == "active",
        "has_confirmed_plan": previous is not None,
        "direct_eligible": direct_eligible,
        "previous_version_id": previous.id if previous is not None else None,
        "mastery": [
            {
                "knowledge_point_id": point.id,
                "display_name": point.display_name,
                "mastery": mastery.evaluate_point_mastery(db, point).to_dict(),
            }
            for point in points
        ],
    }


@router.post("/sources/{source_id}/next-plan", status_code=201)
def next_plan(source_id: str, payload: SuggestPlanRequest, db: Session = Depends(get_db)):
    try:
        assessment = assessment_service.snapshot_for_plan(
            db, source_id, payload.page_start, payload.page_end
        )
        return plan_service.suggest_next_plan(db, source_id, payload, assessment=assessment)
    except ValueError as exc:
        code = str(exc)
        if code == "source_not_found":
            raise fail(410, "source_not_found", "资料已删除或不可用，无法生成复习计划")
        if code == "previous_version_mismatch":
            raise fail(409, code, "上一版计划不属于这份资料")
        raise


@router.get("/citations/{citation_id}")
def get_citation(citation_id: str, db: Session = Depends(get_db)):
    citation = db.get(Citation, citation_id)
    if citation is None:
        raise fail(404, "not_found", "引用不存在")
    if not citation_in_pack(db, citation):
        raise fail(404, "not_found", "引用不属于当前学习计划")
    source = db.get(SourceMaterial, citation.source_id)
    evidence = db.get(Evidence, citation.evidence_id)
    if source is None or source.status != "active":
        payload = _citation_payload(db, citation)
        payload["status"] = "source_deleted"
        payload["asset_url"] = None
        return payload
    if not locator_valid(citation):
        payload = _citation_payload(db, citation)
        payload["status"] = "locator_invalid"
        payload["asset_url"] = None
        payload["message"] = "引用定位失败，已保留摘要"
        return payload
    payload = _citation_payload(db, citation)
    payload["context_before"] = evidence.context_before if evidence else None
    payload["context_after"] = evidence.context_after if evidence else None
    try:
        source_path = storage.absolute(source.stored_path)
        source_exists = source_path.exists()
    except Exception:
        source_exists = False
    if not source_exists:
        payload["status"] = "source_missing"
        payload["asset_url"] = None
        payload["message"] = "原始文件缺失，已保留可核对的文字摘要"
        return payload
    if payload.get("asset_expected") and not payload.get("asset_url"):
        payload["status"] = "asset_missing"
        payload["message"] = "引用页图或媒体文件缺失，已保留可核对的文字摘要"
        return payload
    if payload.get("asset_url"):
        payload["status"] = "ok"
        return payload
    payload["status"] = "ok"
    return payload


@router.get("/citations/{citation_id}/asset")
def get_citation_asset(citation_id: str, db: Session = Depends(get_db)):
    citation = db.get(Citation, citation_id)
    if citation is None:
        raise fail(404, "not_found", "引用不存在")
    if not citation_in_pack(db, citation):
        raise fail(404, "not_found", "引用不属于当前学习计划")
    source = db.get(SourceMaterial, citation.source_id)
    if source is None or source.status != "active":
        raise fail(410, "source_deleted", "来源资料已删除，无法打开原文或预览")
    try:
        source_path = storage.absolute(source.stored_path)
        if not source_path.exists():
            raise FileNotFoundError(source.stored_path)
    except Exception:
        raise fail(410, "source_missing", "原始文件缺失，不再提供旧页图或原文")
    if not locator_valid(citation):
        raise fail(404, "locator_invalid", "引用定位信息失效")
    evidence = db.get(Evidence, citation.evidence_id)
    asset, _asset_kind, _scanned = _resolve_citation_media(db, citation, source, evidence)
    if asset is not None:
        if not (asset.mime or "").startswith("image/"):
            raise fail(404, "asset_missing", "引用资产不是图像文件")
        path = storage.absolute(asset.rel_path)
        if not path.exists():
            raise fail(404, "asset_missing", "引用页图或媒体文件缺失")
        return FileResponse(path, media_type=asset.mime, headers={"Cache-Control": "no-store"})
    if evidence is not None and evidence.kind == "text_span" and source.kind != "pdf":
        path = storage.absolute(source.stored_path)
        if not path.exists():
            raise fail(410, "source_missing", "原始文件缺失")
        return FileResponse(path, media_type=source.mime, headers={"Cache-Control": "no-store"})
    raise fail(404, "asset_missing", "引用页图或媒体文件缺失，仅保留文字摘要")