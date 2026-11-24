from __future__ import annotations

import hashlib
import json
import logging
import math
import hashlib
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT, settings
from app.domain.knowledge import (
    ensure_knowledge_point,

    is_generic_point,
    normalize_point_name,
)
from app.domain.models import (
    Attempt,
    AttemptAnswer,
    Citation,
    Evidence,
    GenerationBatchCache,
    GenerationEvidence,
    GenerationJob,
    HandoutSection,
    ImageAsset,
    KnowledgePoint,
    LearningPack,
    PackPlanVersion,
    PackSource,
    ProgressSnapshot,
    Question,
    QuestionKnowledgePoint,
    SourceMaterial,
    ReviewFocus,
    now_iso,
)
from app.domain.schemas import CitationRef, ModelOutput, RefMap, RefTarget, stem_hash, word_count
from app.domain.validation import ValidationError, validate_output
from app.infra import image_engine, source_service, storage
from app.infra.db import SessionLocal
from app.infra.ids import new_id
from app.infra.model_client import (
    FakeModelClient,
    ImagePayload,
    ModelError,
    ModelRequest,
    TextPayload,
    fit_request,
    get_model_client,
    is_fake_client,
)

logger = logging.getLogger("xunzhi.generation")

PROMPT_VERSION = "prompt_v5"


@dataclass
class Assembly:
    ref_map: RefMap = field(default_factory=RefMap)
    text_items: list[TextPayload] = field(default_factory=list)
    images: list[ImagePayload] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)


def _project_root():
    return PROJECT_ROOT


def load_system_prompt() -> str:
    path = _project_root() / "app" / "prompts" / "system_generate.md"
    return path.read_text(encoding="utf-8")


def _load_asset_image(
    db: Session,
    asset_id: str | None,
    source: SourceMaterial,
    page_no: int | None,
    locator: dict | None = None,
    *,
    strict: bool = True,
):
    asset = db.get(ImageAsset, asset_id) if asset_id else None
    if asset is None and source.kind == "pdf" and page_no is not None:
        asset = source_service.ensure_page_image(db, source, page_no)
    location = f"第 {page_no} 页" if page_no is not None else "页内图像"
    if asset is None or asset.kind == "thumb":
        if not strict:
            return None, None
        raise ValidationError(
            "source_unavailable",
            f"{source.display_name} {location}的图像资产缺失，无法发送，请重新上传或重建资料",
        )
    try:
        path = storage.absolute(asset.rel_path)
        if not path.exists():
            raise FileNotFoundError(asset.rel_path)
        raw = path.read_bytes()
        boxes = (locator or {}).get("boxes") or []
        if boxes:
            raw = image_engine.crop_region(raw, boxes[0])
        data, mime = image_engine.normalize_for_model(raw)
    except Exception as exc:  # noqa: BLE001
        if not strict:
            return None, None
        raise ValidationError(
            "source_unavailable",
            f"{source.display_name} {location}的页图无法读取，请重新上传或重建资料",
        ) from exc
    if not data:
        if not strict:
            return None, None
        raise ValidationError(
            "source_unavailable",
            f"{source.display_name} {location}的页图为空，不能作为图像证据发送",
        )
    return data, mime


def _even_sample(rows: list, quota: int | None) -> list:
    if quota is None:
        return list(rows)
    if quota <= 0:
        return []
    if len(rows) <= quota:
        return list(rows)
    step = len(rows) / quota
    return [rows[min(len(rows) - 1, int(index * step))] for index in range(quota)]


def _text_score(text: str, query_terms: list[str] | None) -> int:
    if not text or not query_terms:
        return 0
    lowered = text.lower()
    score = 0
    for term in query_terms:
        value = (term or "").strip().lower()
        if not value:
            continue
        hits = lowered.count(value)
        if hits:
            score += 10 + hits
    return score


def _page_order(value):
    return (1, 0) if value is None else (0, value)


def _batch_query_terms(payload: dict) -> list[str]:
    constraints = payload.get("plan_constraints") or {}
    terms: list[str] = []
    for module in constraints.get("modules") or []:
        terms.append(module.get("title") or "")
        terms.extend(module.get("core_points") or [])
    for module in constraints.get("review_context") or []:
        terms.append(module.get("title") or "")
        terms.extend(module.get("core_points") or [])
    for item in constraints.get("items") or []:
        terms.append(item.get("display_name") or "")
    terms.append(payload.get("progress_text") or "")
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        value = (term or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _trim_assembly(
    assembly: Assembly,
    request: ModelRequest,
    *,
    aggregate_text: bool = False,
    preserve_labels: bool = False,
) -> Assembly:
    kept = Assembly()
    text_refs = {item.ref for item in request.text_items}
    text_by_ref = {item.ref: item for item in request.text_items}
    image_refs = {item.ref for item in request.images}
    image_by_ref = {item.ref: item for item in assembly.images}
    records = {record["local_ref"]: record for record in assembly.records}
    for ref, target in assembly.ref_map.targets.items():
        record = records.get(ref) or {}
        if ref in image_refs:
            sent_as = "image"
        elif ref in text_refs:
            sent_as = "text"
        elif aggregate_text and target.kind == "text_span":
            sent_as = "text"
        elif preserve_labels and record.get("sent_as") == "label":
            sent_as = "label"
        else:
            continue
        kept.ref_map.targets[ref] = target
        if sent_as == "image" and ref in image_by_ref:
            kept.images.append(image_by_ref[ref])
        if sent_as == "text" and ref in text_by_ref:
            kept.text_items.append(text_by_ref[ref])
        kept.records.append(
            {
                "local_ref": ref,
                "evidence_id": target.evidence_id,
                "kind": target.kind,
                "source_id": target.source_id,
                "sent_as": sent_as,
                "ordinal": len(kept.records) + 1,
            }
        )
    return kept


def _fit_generation_request(
    request: ModelRequest, assembly: Assembly
) -> tuple[ModelRequest, Assembly, dict]:
    fitted, fit = fit_request(
        request,
        model=settings.model,
        max_bytes=settings.model_max_request_mb * 1024 * 1024,
        min_images=1 if request.images else 0,
    )
    trimmed = _trim_assembly(assembly, fitted)
    stats = fit.as_dict()
    stats["refs"] = sorted(trimmed.ref_map.targets)
    stats["dropped_refs"] = sorted(
        set(assembly.ref_map.targets) - set(trimmed.ref_map.targets)
    )
    stats["sources_pages"] = source_locations(trimmed)
    return fitted, trimmed, stats


def _assemble_balanced(
    db: Session,
    sources_spec: list[dict],
    *,
    load_images: bool,
    text_limit: int | None,
    image_limit: int | None,
    query_terms: list[str] | None = None,
) -> Assembly:
    assembly = Assembly()
    text_no = 0
    image_no = 0
    page_no_counter = 0
    prepared: list[tuple[SourceMaterial, list]] = []
    for spec in sources_spec:
        source = db.get(SourceMaterial, spec["source_id"])
        if source is None or source.status != "active":
            raise ValidationError("source_unavailable", "所选资料不可用，请重新选择")
        query = select(Evidence).where(
            Evidence.source_id == source.id, Evidence.status == "active"
        )
        if source.kind == "pdf" and spec.get("page_start"):
            query = query.where(
                Evidence.page_no >= spec["page_start"], Evidence.page_no <= spec["page_end"]
            )
        rows = db.execute(
            query.order_by(Evidence.page_no.is_(None), Evidence.page_no, Evidence.ordinal)
        ).scalars().all()
        prepared.append((source, rows))

    count = len(prepared)
    text_quota = None if text_limit is None else max(0, text_limit // count) if count else 0
    image_quota = None if image_limit is None else max(0, image_limit // count) if count else 0
    text_remainder = 0 if text_limit is None else text_limit - (text_quota or 0) * count
    image_remainder = 0 if image_limit is None else image_limit - (image_quota or 0) * count

    for index, (source, rows) in enumerate(prepared):
        groups: dict[int | None, list] = {}
        for row in rows:
            groups.setdefault(row.page_no, []).append(row)
        pages = sorted(groups, key=_page_order)
        page_quota = (
            None
            if text_quota is None
            else text_quota + (1 if index < text_remainder else 0)
        )
        text_rows = [row for row in rows if row.kind == "text_span"]
        if source.kind in {"text", "md"}:
            fragments = [
                (row, (row.body_text or row.quote_text or "").strip())
                for row in text_rows
            ]
            fragments = [(row, body) for row, body in fragments if body]
            if query_terms:
                ranked = sorted(
                    fragments,
                    key=lambda item: (-_text_score(item[1], query_terms), item[0].ordinal),
                )
                selected = ranked[:page_quota] if page_quota is not None else ranked
            else:
                selected = _even_sample(fragments, page_quota)
            for row, body in selected:
                text_no += 1
                local_ref = f"T{text_no}"
                assembly.text_items.append(
                    TextPayload(
                        ref=local_ref,
                        description=(
                            f"[{local_ref}] {source.display_name}｜文本片段 {row.ordinal}"
                            f"｜{body[:1200]}"
                        ),
                    )
                )
                assembly.ref_map.targets[local_ref] = RefTarget(
                    evidence_id=row.id,
                    source_id=source.id,
                    kind="text_span",
                    page_no=None,
                    confidence=row.confidence,
                    locator_json=row.locator_json,
                    quote_text=body[:400],
                    label_text=row.label_text,
                )
                assembly.records.append(
                    {
                        "local_ref": local_ref,
                        "evidence_id": row.id,
                        "kind": "text_span",
                        "source_id": source.id,
                        "sent_as": "text",
                        "ordinal": len(assembly.records) + 1,
                    }
                )
        else:
            for page_no in _even_sample(pages, page_quota):
                page_rows = [row for row in groups[page_no] if row.kind == "text_span"]
                if not page_rows:
                    continue
                merged = " ".join(
                    (row.body_text or row.quote_text or "").strip() for row in page_rows
                ).strip()
                if not merged:
                    continue
                text_no += 1
                local_ref = f"T{text_no}"
                page = f"｜第 {page_no} 页" if page_no else ""
                assembly.text_items.append(
                    TextPayload(
                        ref=local_ref,
                        description=f"[{local_ref}] {source.display_name}{page}｜{merged[:800]}",
                    )
                )
                base = page_rows[0]
                assembly.ref_map.targets[local_ref] = RefTarget(
                    evidence_id=base.id,
                    source_id=source.id,
                    kind="text_span",
                    page_no=page_no,
                    confidence=base.confidence,
                    locator_json=base.locator_json,
                    quote_text=merged[:400],
                    label_text=base.label_text,
                )
                assembly.records.append(
                    {
                        "local_ref": local_ref,
                        "evidence_id": base.id,
                        "kind": "text_span",
                        "source_id": source.id,
                        "sent_as": "text",
                        "ordinal": len(assembly.records) + 1,
                    }
                )

        image_pages = [
            page
            for page in pages
            if any(row.kind in {"page_image", "image_region"} for row in groups[page])
        ]
        multi_image_pages = len(image_pages) >= 2
        image_candidates = []
        for page_no in image_pages:
            page_rows = [
                row for row in groups[page_no] if row.kind in {"page_image", "image_region"}
            ]
            if not page_rows:
                continue
            page_images = [row for row in page_rows if row.kind == "page_image"]
            regions = [row for row in page_rows if row.kind == "image_region"]
            if multi_image_pages:
                ordered = regions[:2] + page_images[:1]
            else:
                ordered = page_images[:1] + regions[:2]
            image_candidates.extend(ordered)
        image_page_quota = (
            None
            if image_quota is None
            else image_quota + (1 if index < image_remainder else 0)
        )
        for evidence in _even_sample(image_candidates, image_page_quota):
            locator = json.loads(evidence.locator_json)
            if evidence.kind == "page_image":
                page_no_counter += 1
                local_ref = f"P{page_no_counter}"
            else:
                image_no += 1
                local_ref = f"I{image_no}"
            if load_images:
                data, mime = _load_asset_image(
                    db,
                    locator.get("asset_id"),
                    source,
                    evidence.page_no,
                    locator,
                    strict=evidence.kind != "page_image",
                )
                if data:
                    if evidence.kind == "page_image":
                        description = f"【图 {local_ref}】{source.display_name} 第 {evidence.page_no} 页（整页）"
                    else:
                        page_label = f" 第 {evidence.page_no} 页" if evidence.page_no is not None else ""
                        description = (
                            f"【图 {local_ref}】{source.display_name}{page_label}"
                            f"｜{evidence.label_text or '按图中区域作答'}"
                        )
                    assembly.images.append(
                        ImagePayload(ref=local_ref, description=description, data=data, mime=mime)
                    )
                    sent_as = "image"
                else:
                    sent_as = "label"
            else:
                sent_as = "label"
            assembly.ref_map.targets[local_ref] = RefTarget(
                evidence_id=evidence.id,
                source_id=source.id,
                kind=evidence.kind,
                page_no=evidence.page_no,
                confidence=evidence.confidence,
                locator_json=evidence.locator_json,
                quote_text=evidence.quote_text,
                label_text=evidence.label_text,
            )
            assembly.records.append(
                {
                    "local_ref": local_ref,
                    "evidence_id": evidence.id,
                    "kind": evidence.kind,
                    "source_id": source.id,
                    "sent_as": sent_as,
                    "ordinal": len(assembly.records) + 1,
                }
            )
    if not assembly.ref_map.targets:
        raise ValidationError("insufficient_evidence", "本次范围内没有可用证据，请扩大页码或补充资料")
    return assembly


def assemble_for_sources(
    db: Session,
    sources_spec: list[dict],
    *,
    load_images: bool = True,
    text_limit: int | None = None,
    image_limit: int | None = None,
    balanced: bool = False,
    query_terms: list[str] | None = None,
) -> Assembly:
    for spec in sources_spec:
        source = db.get(SourceMaterial, spec.get("source_id"))
        if source is not None:
            source_service.ensure_pdf_region_evidence(db, source)
    if balanced:
        return _assemble_balanced(
            db,
            sources_spec,
            load_images=load_images,
            text_limit=text_limit,
            image_limit=image_limit,
            query_terms=query_terms,
        )
    assembly = Assembly()
    text_no = 0
    image_no = 0
    page_no_counter = 0
    used_pages: set[tuple[str, int]] = set()
    prepared: list[tuple[SourceMaterial, list]] = []
    for spec in sources_spec:
        source = db.get(SourceMaterial, spec["source_id"])
        if source is None or source.status != "active":
            raise ValidationError("source_unavailable", "所选资料不可用，请重新选择")
        query = select(Evidence).where(
            Evidence.source_id == source.id, Evidence.status == "active"
        )
        if source.kind == "pdf" and spec.get("page_start"):
            query = query.where(
                Evidence.page_no >= spec["page_start"], Evidence.page_no <= spec["page_end"]
            )
        rows = db.execute(query.order_by(Evidence.page_no.is_(None), Evidence.page_no, Evidence.ordinal)).scalars().all()
        prepared.append((source, rows))

    for source, rows in prepared:
        for evidence in rows:
            locator = json.loads(evidence.locator_json)
            if evidence.kind == "text_span":
                if text_limit is not None and text_no >= text_limit:
                    continue
                text_no += 1
                local_ref = f"T{text_no}"
                page = f"｜第 {evidence.page_no} 页" if evidence.page_no else ""
                assembly.text_items.append(
                    TextPayload(
                        ref=local_ref,
                        description=f"[{local_ref}] {source.display_name}{page}｜{(evidence.quote_text or '')[:400]}",
                    )
                )
                sent_as = "text"
            elif evidence.kind == "page_image":
                if image_limit is not None and page_no_counter >= image_limit:
                    continue
                page_no = evidence.page_no
                if page_no is None:
                    raise ValidationError(
                        "source_unavailable",
                        f"{source.display_name} 的整页图像缺少物理页码，无法发送",
                    )
                if (source.id, page_no) in used_pages:
                    continue
                used_pages.add((source.id, page_no))
                page_no_counter += 1
                local_ref = f"P{page_no_counter}"
                if load_images:
                    data, mime = _load_asset_image(db, locator.get("asset_id"), source, page_no, locator)
                    assembly.images.append(
                        ImagePayload(
                            ref=local_ref,
                            description=f"【图 {local_ref}】{source.display_name} 第 {page_no} 页（整页）",
                            data=data,
                            mime=mime,
                        )
                    )
                    sent_as = "image"
                else:
                    sent_as = "label"
            else:
                if image_limit is not None and image_no >= image_limit:
                    continue
                image_no += 1
                local_ref = f"I{image_no}" if source.kind == "pdf" else f"F{image_no}"
                if load_images:
                    data, mime = _load_asset_image(
                        db,
                        locator.get("asset_id"),
                        source,
                        evidence.page_no,
                        locator,
                        strict=False,
                    )
                    page_label = (
                        f" 第 {evidence.page_no} 页"
                        if evidence.page_no is not None
                        else ""
                    )
                    if data:
                        assembly.images.append(
                            ImagePayload(
                                ref=local_ref,
                                description=(
                                    f"【图 {local_ref}】{source.display_name}{page_label}"
                                    f"｜{(evidence.label_text or '按图中区域作答')}"
                                ),
                                data=data,
                                mime=mime,
                            )
                        )
                        sent_as = "image"
                    else:
                        sent_as = "label"
                else:
                    sent_as = "label"
            target = RefTarget(
                evidence_id=evidence.id,
                source_id=source.id,
                kind=evidence.kind,
                page_no=evidence.page_no,
                confidence=evidence.confidence,
                locator_json=evidence.locator_json,
                quote_text=evidence.quote_text,
                label_text=evidence.label_text,
            )
            assembly.ref_map.targets[local_ref] = target
            assembly.records.append(
                {
                    "local_ref": local_ref,
                    "evidence_id": evidence.id,
                    "kind": evidence.kind,
                    "source_id": source.id,
                    "sent_as": sent_as,
                    "ordinal": len(assembly.records) + 1,
                }
            )
    if not assembly.ref_map.targets:
        raise ValidationError("insufficient_evidence", "本次范围内没有可用证据，请扩大页码或补充资料")
    return assembly


def source_locations(assembly: Assembly) -> list[tuple[str, int]]:
    return sorted(
        {
            (target.source_id, target.page_no)
            for target in assembly.ref_map.targets.values()
            if target.page_no is not None
        }
    )


def assemble_evidence(db: Session, job: GenerationJob) -> Assembly:
    request = json.loads(job.request_json)
    constraints = request.get("plan_constraints") or {}
    balanced = len(request.get("sources") or []) > 1 or bool(constraints.get("modules_required"))
    assembly = assemble_for_sources(
        db,
        request["sources"],
        load_images=True,
        text_limit=settings.model_max_text_items,
        image_limit=settings.model_max_images,
        balanced=balanced,
        query_terms=_batch_query_terms(request),
    )
    text_bytes = sum(len(item.description.encode("utf-8")) for item in assembly.text_items)
    image_bytes = sum(len(item.data) for item in assembly.images)
    locations = source_locations(assembly)
    logger.info(
        "model evidence stats job=%s text_items=%s images=%s text_bytes=%s image_bytes=%s total_bytes=%s sources_pages=%s",
        job.id,
        len(assembly.text_items),
        len(assembly.images),
        text_bytes,
        image_bytes,
        text_bytes + image_bytes,
        locations,
    )
    return assembly


def store_assembly(db: Session, job_id: str, assembly: Assembly) -> None:
    for record in assembly.records:
        db.add(GenerationEvidence(job_id=job_id, **record))


def build_request_for(payload: dict, assembly: Assembly) -> ModelRequest:
    focus = "；".join(payload.get("review_focus") or []) or "（无）"
    parent_note = ""
    if payload.get("parent_pack_id"):
        parent_note = "\n【再练约束】本次针对上一轮错题，题干不得与上一轮相同或仅更换选项顺序。"
    point_map = ""
    focus_points = payload.get("focus_points") or []
    if focus_points:
        names = "；".join(
            str(item.get("display_name") or "") for item in focus_points if item.get("display_name")
        )
        if names:
            point_map = f"\n【本轮考点名称】{names}。每题 knowledge_point 必须使用其中一个名称。"
    plan_note = ""
    constraints = payload.get("plan_constraints") or {}
    if constraints.get("items"):
        point_rows = [
            {
                "knowledge_point_id": item.get("knowledge_point_id"),
                "display_name": item.get("display_name"),
                "module_id": item.get("module_id"),
                "source_id": item.get("source_id"),
                "page_start": item.get("page_start"),
                "page_end": item.get("page_end"),
                "suggested_question_count": item.get("suggested_question_count"),
            }
            for item in constraints.get("items") or []
        ]
        excluded = "、".join(
            constraints.get("excluded_names") or constraints.get("excluded") or []
        ) or "（无）"
        plan_note = (
            f"\n【本轮计划约束】计划版本={constraints.get('plan_version_id')}；"
            f"总题量={constraints.get('total_question_count')}；考点清单："
            + json.dumps(point_rows, ensure_ascii=False)
            + f"；排除考点：{excluded}，不得出题或展开。"
            "每题必须返回 knowledge_point_id，且只能使用清单中的一个稳定 ID；"
            "knowledge_point 显示名必须与清单中同一 ID 的名称一致。"
            "不得只按名称猜测考点；同名考点以 knowledge_point_id 区分。"
        )
        module_plan = constraints.get("modules") or []
        if constraints.get("modules_required") and module_plan:
            compact = [
                {
                    "module_id": module.get("module_id"),
                    "title": module.get("title"),
                    "objective": module.get("objective"),
                    "core_points": module.get("core_points"),
                    "knowledge_point_ids": module.get("knowledge_point_ids") or [],
                    "point_allocation": module.get("point_allocation") or [],
                    "source_names": module.get("source_names") or [],
                    "source_scopes": module.get("source_scopes") or [],
                    "page_start": module.get("page_start"),
                    "page_end": module.get("page_end"),
                    "completion": module.get("completion_standard"),
                    "question_count": module.get("suggested_question_count"),
                }
                for module in module_plan
            ]
            plan_note += (
                "\n【模块计划】\n"
                + json.dumps(compact, ensure_ascii=False)
                + "\n必须输出同数量的 modules，模块 module_id 与上表一致；每模块的 key_points / pitfalls / visual 讲义按「讲清机制 → 演示一个例子 → 解释容易混淆的地方 → 练一题」组织：至少一条 key_point 的 heading 写「机制：<概念>」，用正文讲清过程、判断依据和原因；至少一条 key_point 的 heading 写「例子：<场景>」，给出可自行构造并标注为教学示例的具体例子；pitfalls 用「易混淆：<点>」说明容易混淆的地方；visual 可放短教学图例。不要反复写「资料原文写明」「资料指出」「原文提到」；直接解释机制，资料只用于约束主题和事实。模块标题必须是完整概念，不要拼文件名、页码或孤立片段；若核心概念是 IP/CIDR，写成「如何理解 192.168.1.0/24」。例如交换机转发帧：说明帧从哪个端口进入、交换机查什么表、目的 MAC 已知和未知时分别从哪些端口发出。每道题必须带 module_id（属于对应模块）和 knowledge_point_id（来自考点清单）；多模块时必须在整包 questions 中至少写出 1 道 module_id=review 的跨模块应用或对比题（可从一道题改写为跨模块对比），没有 review 题会被判为失败。"
                + "不能只输出目录；缺少模块讲义或模块练习会被判为失败。"
            )
            if constraints.get("review_required"):
                review_context = constraints.get("review_context") or []
                plan_note += (
                    "\n【本批综合复习】必须把本批至少 1 道题改写为 module_id=review 的跨模块应用或对比题；"
                    "总题量不变，这道题仍计入对应考点，并返回该考点在清单中的 knowledge_point_id。"
                )
                if review_context:
                    plan_note += (
                        "综合复习题要同时关联本批模块与以下已确认模块的关键概念："
                        + json.dumps(
                            [
                                {
                                    "module_id": module.get("module_id"),
                                    "title": module.get("title"),
                                    "core_points": module.get("core_points") or [],
                                }
                                for module in review_context
                            ],
                            ensure_ascii=False,
                        )
                        + "。对比所需的原文证据已放入本次清单，引用只能使用本次清单中的编号。"
                    )
    assessment = constraints.get("assessment") or {}
    assessment_note = ""
    if assessment:
        notes = []
        for finding in (assessment.get("findings") or [])[:4]:
            notes.append(
                f"{finding.get('knowledge_point') or finding.get('knowledge_point_id')}："
                f"{finding.get('judgement') or ''}"
            )
        for step in (assessment.get("route_steps") or [])[:4]:
            notes.append(
                f"{step.get('knowledge_point') or step.get('knowledge_point_id')}："
                f"{step.get('action_label') or ''} {step.get('instruction') or ''}"
                f"（完成标准：{step.get('completion') or ''}）"
            )
        if notes:
            assessment_note = (
                "\n【个人档案重点】" + "；".join(notes)
                + "。讲义和题目必须落实这些重点；题量、资料范围与排除项仍以本轮计划为准。"
            )
    retry_note = ""
    if payload.get("retry_note"):
        retry_note = f"\n【本次修正】{payload.get('retry_note')}"
    task = (
        f"【任务】难度={payload.get('difficulty')}；题量={payload.get('question_count')}；\n"
        f"【学生进度原文】{payload.get('progress_text')}\n"
        f"【资料范围】{payload.get('scope_text')}\n"
        f"【本轮复习重点】{focus}{parent_note}{point_map}{plan_note}{assessment_note}{retry_note}\n"
        "请严格按给定证据输出 JSON。引用只能使用本次清单中的编号，逐字使用清单左侧编号，不要加方括号、标点或自行改号。"
    )
    return ModelRequest(
        system_prompt=load_system_prompt(),
        task_text=task,
        text_items=assembly.text_items,
        images=assembly.images,
        question_count=int(payload.get("question_count") or 5),
    )


def build_request(job: GenerationJob, assembly: Assembly) -> ModelRequest:
    return build_request_for(json.loads(job.request_json), assembly)


BATCH_QUESTION_MAX = 8
BATCH_MODULE_MAX = 1


def _should_batch(constraints: dict) -> bool:
    if not constraints.get("modules_required"):
        return False
    modules = constraints.get("modules") or []
    total = int(constraints.get("total_question_count") or 0)
    return len(modules) >= 2 and total > BATCH_QUESTION_MAX


def _split_batches(constraints: dict) -> list[dict]:
    modules = constraints.get("modules") or []
    parts: list[tuple[dict, list[str], int]] = []
    for module in modules:
        point_ids = [pid for pid in module.get("knowledge_point_ids") or [] if pid]
        if not point_ids:
            continue
        allocation = {
            item.get("knowledge_point_id"): int(item.get("suggested_question_count") or 0)
            for item in module.get("point_allocation") or []
        }
        counts = {pid: allocation.get(pid, 0) for pid in point_ids}
        total = sum(counts.values())
        if total <= 0:
            total = int(module.get("suggested_question_count") or 0)
            if total > 0:
                base, extra = divmod(total, len(point_ids))
                counts = {
                    pid: base + (1 if index < extra else 0)
                    for index, pid in enumerate(point_ids)
                }
        if total <= 0:
            parts.append((module, point_ids, 0))
            continue
        current_ids: list[str] = []
        current_count = 0
        for pid in point_ids:
            count = counts.get(pid, 0)
            if count <= 0:
                continue
            if current_ids and current_count + count > BATCH_QUESTION_MAX:
                parts.append((module, current_ids, current_count))
                current_ids = []
                current_count = 0
            current_ids.append(pid)
            current_count += count
        if current_ids:
            parts.append((module, current_ids, current_count))

    batches: list[list[dict]] = []
    current: list[dict] = []
    for module, point_ids, count in parts:
        part_module = {
            **module,
            "knowledge_point_ids": point_ids,
            "suggested_question_count": count,
            "point_allocation": [
                item
                for item in module.get("point_allocation") or []
                if item.get("knowledge_point_id") in point_ids
            ],
        }
        if current and (
            len(current) >= BATCH_MODULE_MAX
            or sum(item["question_count"] for item in current) + count > BATCH_QUESTION_MAX
        ):
            batches.append(current)
            current = []
        current.append(
            {
                "module": part_module,
                "point_ids": point_ids,
                "question_count": count,
                "source_ids": list(module.get("source_ids") or []),
            }
        )
    if current:
        batches.append(current)

    result = []
    for index, rows in enumerate(batches, start=1):
        source_ids = sorted({sid for row in rows for sid in row["source_ids"]})
        result.append(
            {
                "index": index,
                "rows": rows,
                "module_ids": [row["module"].get("module_id") for row in rows],
                "point_ids": {pid for row in rows for pid in row["point_ids"]},
                "question_count": sum(row["question_count"] for row in rows),
                "source_ids": source_ids,
            }
        )
    return result


def _batch_payload(payload: dict, batch: dict, *, last: bool) -> dict:
    constraints = payload.get("plan_constraints") or {}
    modules = constraints.get("modules") or []
    multi_module = len(modules) >= 2
    review_context: list[dict] = []
    if last and multi_module:
        batch_module_ids = set(batch["module_ids"])
        review_context = [
            {
                "module_id": module.get("module_id"),
                "title": module.get("title") or "",
                "core_points": module.get("core_points") or [],
            }
            for module in modules
            if module.get("module_id") not in batch_module_ids
        ]
    point_ids = batch["point_ids"]
    sub_constraints = {
        **constraints,
        "items": [
            item
            for item in constraints.get("items") or []
            if item.get("knowledge_point_id") in point_ids
        ],
        "modules": [row["module"] for row in batch["rows"]],
        "total_question_count": batch["question_count"],
        "modules_required": True,
        "review_required": bool(last) and multi_module,
    }
    if review_context:
        sub_constraints["review_context"] = review_context
    if last and multi_module:
        allowed = {
            source_id
            for module in modules
            for source_id in (module.get("source_ids") or [])
        } or set(batch["source_ids"])
    else:
        allowed = set(batch["source_ids"])
    sources = [
        spec
        for spec in payload.get("sources") or []
        if not allowed or spec.get("source_id") in allowed
    ] or list(payload.get("sources") or [])
    return {
        **payload,
        "sources": sources,
        "question_count": batch["question_count"],
        "plan_constraints": sub_constraints,
    }


def _rewrite_output_refs(output: ModelOutput, rewrite: dict[str, str]) -> None:
    for block in output.handout.key_points + output.handout.pitfalls + output.handout.visual:
        for citation in block.citations:
            citation.ref = rewrite.get(citation.ref, citation.ref)
    for module in output.modules:
        for block in module.key_points + module.pitfalls + module.visual:
            for citation in block.citations:
                citation.ref = rewrite.get(citation.ref, citation.ref)
        _rewrite_page_refs(module, rewrite)
    for question in output.questions:
        for citation in question.citations:
            citation.ref = rewrite.get(citation.ref, citation.ref)


def _rewrite_page_refs(module, rewrite: dict[str, str]) -> None:
    for page in module.pages:
        for citation in page.related_sources:
            citation.ref = rewrite.get(citation.ref, citation.ref)
        if page.visual is not None and page.visual.source_ref:
            page.visual.source_ref = rewrite.get(page.visual.source_ref, page.visual.source_ref)


def _normalize_review_modules(
    output: ModelOutput, point_map: dict[int, str], constraints: dict
) -> None:
    module_of_point = {
        point_id: module.get("module_id")
        for module in constraints.get("modules") or []
        for point_id in module.get("knowledge_point_ids") or []
    }
    reviews = [question for question in output.questions if question.module_id == "review"]
    for question in reviews[:-1]:
        module_id = module_of_point.get(point_map.get(question.ordinal))
        if module_id:
            question.module_id = module_id


EVIDENCE_RULE_VERSION = "evidence_v3"
BATCH_CACHE_VERSION = 6


class BatchConflict(Exception):
    def __init__(self, batch_index: int, message: str):
        super().__init__(message)
        self.batch_index = batch_index
        self.message = message


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_plan_key(payload: dict) -> str:
    version = payload.get("plan_version_id")
    if version:
        return f"plan:{version}"
    key = payload.get("idempotency_key") or ""
    return f"chain:{key.split(':retry:')[0]}"


def _module_signature(constraints: dict) -> list[dict]:
    modules = []
    for module in constraints.get("modules") or []:
        modules.append(
            {
                "module_id": module.get("module_id"),
                "title": module.get("title") or "",
                "knowledge_point_ids": sorted(module.get("knowledge_point_ids") or []),
                "suggested_question_count": int(module.get("suggested_question_count") or 0),
            }
        )
    return sorted(modules, key=lambda item: item.get("module_id") or "")


def _item_signature(constraints: dict) -> list[dict]:
    items = []
    for item in constraints.get("items") or []:
        items.append(
            {
                "knowledge_point_id": item.get("knowledge_point_id"),
                "display_name": item.get("display_name") or "",
                "suggested_question_count": int(item.get("suggested_question_count") or 0),
                "module_id": item.get("module_id"),
            }
        )
    return sorted(items, key=lambda item: item.get("knowledge_point_id") or "")


def _batch_signature(db: Session, payload: dict, batch: dict, assembly: Assembly) -> str:
    constraints = payload.get("plan_constraints") or {}
    sources = []
    for spec in payload.get("sources") or []:
        source = db.get(SourceMaterial, spec.get("source_id"))
        sources.append(
            {
                "source_id": spec.get("source_id"),
                "page_start": spec.get("page_start"),
                "page_end": spec.get("page_end"),
                "kind": source.kind if source is not None else None,
                "sha256": source.sha256 if source is not None else None,
            }
        )
    evidence = sorted(target.evidence_id for target in assembly.ref_map.targets.values())
    material = {
        "cache_version": BATCH_CACHE_VERSION,
        "plan_version_id": payload.get("plan_version_id"),
        "batch_index": batch["index"],
        "batch_modules": [row["module"].get("module_id") for row in batch["rows"]],
        "question_count": batch["question_count"],
        "modules": _module_signature(constraints),
        "items": _item_signature(constraints),
        "sources": sorted(sources, key=lambda item: item.get("source_id") or ""),
        "evidence_ids": evidence,
        "model": settings.model,
        "prompt_version": PROMPT_VERSION,
        "evidence_rule_version": EVIDENCE_RULE_VERSION,
        "budget": {
            "request_mb": settings.model_max_request_mb,
            "text_items": settings.model_max_text_items,
            "images": settings.model_max_images,
        },
        "refs": sorted(assembly.ref_map.targets),
        "query_terms": _batch_query_terms(payload),
    }
    return _sha256_text(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _batch_cache_key(payload: dict, batch: dict, fingerprint: str) -> str:
    return _sha256_text(f"{_stable_plan_key(payload)}|{fingerprint}|{batch['index']}")


def _ref_map_payload(assembly: Assembly) -> dict:
    sent_as = {record["local_ref"]: record.get("sent_as") for record in assembly.records}
    return {
        "targets": {
            ref: {
                "evidence_id": target.evidence_id,
                "source_id": target.source_id,
                "kind": target.kind,
                "page_no": target.page_no,
                "confidence": target.confidence,
                "locator_json": target.locator_json,
                "quote_text": target.quote_text,
                "label_text": target.label_text,
            }
            for ref, target in assembly.ref_map.targets.items()
        },
        "sent_as": sent_as,
    }


def _restore_assembly(data: dict) -> Assembly:
    assembly = Assembly()
    if "targets" in data:
        targets = data.get("targets") or {}
        sent_map = data.get("sent_as") or {}
    else:
        targets = data
        sent_map = {}
    for ref, fields in targets.items():
        target = RefTarget(**fields)
        assembly.ref_map.targets[ref] = target
        sent_as = sent_map.get(ref) or ("text" if target.kind == "text_span" else "image")
        assembly.records.append(
            {
                "local_ref": ref,
                "evidence_id": target.evidence_id,
                "kind": target.kind,
                "source_id": target.source_id,
                "sent_as": sent_as,
                "ordinal": len(assembly.records) + 1,
            }
        )
    return assembly


def _copy_assembly(assembly: Assembly) -> Assembly:
    copied = Assembly()
    for ref, target in assembly.ref_map.targets.items():
        copied.ref_map.targets[ref] = RefTarget(
            evidence_id=target.evidence_id,
            source_id=target.source_id,
            kind=target.kind,
            page_no=target.page_no,
            confidence=target.confidence,
            locator_json=target.locator_json,
            quote_text=target.quote_text,
            label_text=target.label_text,
        )
    copied.records = [dict(record) for record in assembly.records]
    return copied


def _load_batch_cache(db: Session, cache_key: str) -> tuple[ModelOutput, Assembly] | None:
    row = db.execute(
        select(GenerationBatchCache).where(GenerationBatchCache.cache_key == cache_key)
    ).scalars().first()
    if row is None:
        return None
    try:
        output = ModelOutput.model_validate_json(row.batch_json)
        data = json.loads(row.ref_map_json)
        assembly = _restore_assembly(data)
        if not assembly.ref_map.targets:
            raise ValueError("empty ref map")
        for target in assembly.ref_map.targets.values():
            evidence = db.get(Evidence, target.evidence_id)
            source = db.get(SourceMaterial, target.source_id)
            if evidence is None or evidence.status != "active":
                raise ValueError("evidence unavailable")
            if source is None or source.status != "active":
                raise ValueError("source unavailable")
            if evidence.source_id != source.id:
                raise ValueError("source mismatch")
            if evidence.page_no != target.page_no:
                raise ValueError("page mismatch")
        return output, assembly
    except Exception as exc:  # noqa: BLE001
        db.delete(row)
        db.commit()
        logger.warning("batch cache invalid key=%s error=%s", cache_key, exc)
        return None


def _save_batch_cache(
    db: Session,
    job: GenerationJob,
    payload: dict,
    batch: dict,
    fingerprint: str,
    cache_key: str,
    output: ModelOutput,
    assembly: Assembly,
    usage: dict,
) -> bool:
    if not _job_is_running(db, job.id):
        return False
    db.add(
        GenerationBatchCache(
            id=new_id("batch_cache"),
            cache_key=cache_key,
            plan_version_id=payload.get("plan_version_id"),
            job_id=job.id,
            batch_index=batch["index"],
            fingerprint=fingerprint,
            batch_json=output.model_dump_json(),
            ref_map_json=json.dumps(_ref_map_payload(assembly), ensure_ascii=False),
            usage_json=json.dumps(usage, ensure_ascii=False),
            created_at=now_iso(),
            completed_at=now_iso(),
        )
    )
    db.commit()
    return True


def _delete_batch_cache(db: Session, cache_key: str) -> None:
    row = db.execute(
        select(GenerationBatchCache).where(GenerationBatchCache.cache_key == cache_key)
    ).scalars().first()
    if row is not None:
        db.delete(row)
        db.commit()


def _drop_stale_review_cache(
    db: Session, payload: dict, batch: dict, *, review_required: bool
) -> None:
    plan_version_id = payload.get("plan_version_id")
    if not review_required or not plan_version_id:
        return
    rows = db.execute(
        select(GenerationBatchCache).where(
            GenerationBatchCache.plan_version_id == plan_version_id,
            GenerationBatchCache.batch_index == batch["index"],
        )
    ).scalars().all()
    removed = 0
    for row in rows:
        try:
            output = ModelOutput.model_validate_json(row.batch_json)
        except PydanticValidationError:
            output = None
        if output is not None and any(item.module_id == "review" for item in output.questions):
            continue
        db.delete(row)
        removed += 1
    if removed:
        db.commit()
        logger.info(
            "generation batch stale review cache dropped plan=%s batch=%s rows=%s",
            plan_version_id,
            batch["index"],
            removed,
        )


def _execute_batch(
    db: Session,
    job: GenerationJob,
    payload: dict,
    batch: dict,
    client,
    *,
    force: bool = False,
    model_semaphore=None,
) -> tuple[ModelOutput, Assembly, dict, str, bool]:
    started_at = time.monotonic()
    logger.info(
        "generation batch start job=%s batch=%s at=%s",
        job.id,
        batch["index"],
        round(started_at, 6),
    )
    total_batches = len(_split_batches(payload.get("plan_constraints") or {}))
    sub_payload = _batch_payload(payload, batch, last=batch["index"] == total_batches)
    review_required = bool((sub_payload.get("plan_constraints") or {}).get("review_required"))
    text_limit = max(16, min(settings.model_max_text_items, batch["question_count"] * 4 + 8))
    image_limit = max(1, min(settings.model_max_images, batch["question_count"]))
    assembly = assemble_for_sources(
        db,
        sub_payload.get("sources") or [],
        load_images=True,
        text_limit=text_limit,
        image_limit=image_limit,
        balanced=True,
        query_terms=_batch_query_terms(sub_payload),
    )
    request = build_request_for(sub_payload, assembly)
    request, assembly, fit_stats = _fit_generation_request(request, assembly)
    fingerprint = _batch_signature(db, sub_payload, batch, assembly)
    cache_key = _batch_cache_key(sub_payload, batch, fingerprint)
    if not force:
        cached = _load_batch_cache(db, cache_key)
        if cached is not None:
            cached_output, cached_assembly = cached
            try:
                _validate_batch(db, cached_output, sub_payload, cached_assembly)
            except (ValidationError, PydanticValidationError) as exc:
                _delete_batch_cache(db, cache_key)
                logger.warning(
                    "generation batch cache rejected job=%s batch=%s reason=%s",
                    job.id,
                    batch["index"],
                    getattr(exc, "message", str(exc)),
                )
            else:
                logger.info(
                    "generation batch cache hit job=%s batch=%s/%s key=%s reason=fingerprint_match",
                    job.id,
                    batch["index"],
                    total_batches,
                    cache_key,
                )
                return cached_output, cached_assembly, {}, cache_key, True
    _drop_stale_review_cache(db, payload, batch, review_required=review_required)
    last_error = None
    for attempt in range(1, 4):
        if not _job_is_running(db, job.id):
            raise ValidationError("cancelled", "任务已取消")
        attempt_payload = sub_payload
        attempt_assembly = assembly
        attempt_fingerprint = fingerprint
        attempt_cache_key = cache_key
        attempt_request = request
        attempt_fit_stats = fit_stats
        retry_note = None
        last_code = getattr(last_error, "code", "")
        if attempt > 1 and last_code == "module_review_missing" and review_required:
            retry_note = (
                "上一次输出缺少综合复习题。本次必须把至少 1 道题改写为 module_id=review 的跨模块应用或对比题；"
                "总题量与考点分配不变，这道题仍计入对应考点，引用只能使用本次清单中的编号。"
            )
        elif attempt > 1 and last_code in {"module_pages_missing", "module_source_asset_missing"}:
            retry_note = (
                "上一次输出缺少模块学习页 pages，或缺少对照多页图像所需的资料原图页。"
                "本次必须为每个模块输出有序 pages（至少 1 页），每页包含 page_id、"
                "knowledge_point_ids（本模块考点稳定 ID）、title、explanation、"
                "可选 visual（text/source_asset/comparison/step_sequence）与 related_sources；同模块相邻且使用同一实际依据、可以连续讲解的学习页应合并为一页，用段落或小标题保留各知识点；不同条件图、独立互动和模块回顾保持独立。同模块相邻且使用同一实际依据、可以连续讲解的学习页应合并为一页，用段落或小标题保留各知识点；不同条件图、独立互动和模块回顾保持独立。"
                "若资料含多个物理页的图像且目标要求对照，必须为每个物理页分别输出一页 "
                "source_asset，source_ref 只能使用本次实际发送的图像编号。"
            )
        if retry_note:
            attempt_payload = {**sub_payload, "retry_note": retry_note}
            attempt_request = build_request_for(attempt_payload, assembly)
            attempt_request, attempt_assembly, attempt_fit_stats = _fit_generation_request(
                attempt_request, assembly
            )
            attempt_fingerprint = _batch_signature(db, attempt_payload, batch, attempt_assembly)
            attempt_cache_key = _batch_cache_key(attempt_payload, batch, attempt_fingerprint)
        try:
            if model_semaphore is not None:
                with model_semaphore:
                    result = _call_model(client, attempt_request)
            else:
                result = _call_model(client, attempt_request)
            stats = dict(attempt_fit_stats)
            stats["job_id"] = job.id
            stats["batch"] = batch["index"]
            logger.info("generation batch sent job=%s %s", job.id, stats)
            output = _parse_output(result, result.finish_reason == "length")
            _validate_batch(db, output, attempt_payload, attempt_assembly)
            usage = dict(getattr(result, "usage", {}) or {})
            saved = _save_batch_cache(
                db,
                job,
                attempt_payload,
                batch,
                attempt_fingerprint,
                attempt_cache_key,
                output,
                attempt_assembly,
                usage,
            )
            logger.info(
                "generation batch ok job=%s batch=%s/%s questions=%s attempt=%s saved=%s at=%s",
                job.id,
                batch["index"],
                total_batches,
                len(output.questions),
                attempt,
                saved,
                round(time.monotonic(), 6),
            )
            return output, attempt_assembly, usage, attempt_cache_key, False
        except (ModelError, ValidationError, PydanticValidationError) as exc:
            last_error = exc
            if (
                getattr(exc, "code", "") in {"upstream_rate_limited", "rate_limited"}
                and attempt < 3
            ):
                time.sleep(min(2 ** attempt, 8))
            logger.warning(
                "generation batch failed job=%s batch=%s/%s attempt=%s code=%s message=%s",
                job.id,
                batch["index"],
                total_batches,
                attempt,
                getattr(exc, "code", "validation_error"),
                getattr(exc, "message", str(exc)),
            )
    last_code = getattr(last_error, "code", "invalid_structure")
    last_message = getattr(last_error, "message", str(last_error))
    if last_code == "module_review_missing":
        raise ValidationError(
            "module_review_missing",
            f"综合复习题生成失败：负责综合复习的第 {batch['index']} 批连续重试仍未生成 module_id=review 的跨模块题目。"
            "可点击「重试生成」继续，已通过的批次会复用；若仍失败，请返回诊断调整学习方案或减少模块。",
        ) from last_error
    raise ValidationError(
        last_code,
        f"第 {batch['index']} 批生成失败：{last_message}",
    ) from last_error


def _merge_batches(items: list[tuple[int, ModelOutput, Assembly]], constraints: dict) -> tuple[ModelOutput, Assembly]:
    first = items[0][1]
    merged = ModelOutput(
        progress_reading=first.progress_reading.model_copy(deep=True),
        handout=first.handout.model_copy(deep=True),
        modules=[],
        questions=[],
    )
    merged.handout.scope.covered = [
        module.get("title") for module in constraints.get("modules") or [] if module.get("title")
    ] or merged.handout.scope.covered
    merged_assembly = Assembly()
    counters = {"T": 0, "P": 0, "I": 0, "F": 0}
    seen_modules: set[str] = set()
    seen_stems: set[str] = set()
    ordinal = 0
    for batch_index, output, assembly in items:
        rewrite: dict[str, str] = {}
        for old_ref, target in assembly.ref_map.targets.items():
            prefix = old_ref[0] if old_ref and old_ref[0] in "TPIF" else (
                "T" if target.kind == "text_span" else "I"
            )
            counters[prefix] += 1
            new_ref = f"{prefix}{counters[prefix]}"
            rewrite[old_ref] = new_ref
            merged_assembly.ref_map.targets[new_ref] = target
            merged_assembly.records.append(
                {
                    "local_ref": new_ref,
                    "evidence_id": target.evidence_id,
                    "kind": target.kind,
                    "source_id": target.source_id,
                    "sent_as": "text" if target.kind == "text_span" else "image",
                    "ordinal": len(merged_assembly.records) + 1,
                }
            )
        _rewrite_output_refs(output, rewrite)
        for module in output.modules:
            if module.module_id in seen_modules:
                continue
            seen_modules.add(module.module_id)
            merged.modules.append(module)
        for question in output.questions:
            digest = stem_hash(question.stem)
            if digest in seen_stems:
                raise BatchConflict(
                    batch_index,
                    "分批生成合并后出现重复题目，需重跑冲突批次",
                )
            seen_stems.add(digest)
            ordinal += 1
            question.ordinal = ordinal
            merged.questions.append(question)
    if not merged_assembly.ref_map.targets:
        raise ValidationError("insufficient_evidence", "分批生成没有可用证据，请扩大页码范围")
    return merged, merged_assembly


def _comparison_requested(payload: dict) -> bool:
    text = f"{payload.get('progress_text') or ''} {payload.get('scope_text') or ''}"
    return any(
        token in text
        for token in ("对比", "比较", "对照", "两页", "两张", "两个条件", "分别")
    )


def _require_source_asset_pages(
    output: ModelOutput,
    constraints: dict,
    assembly: Assembly,
    sent_image_refs: set[str],
    payload: dict,
) -> None:
    """目标明确要求对照多页图时，每个相关图像物理页都要有独立的资料原图学习页。"""
    if not _comparison_requested(payload):
        return
    required = {
        (target.source_id, target.page_no)
        for ref, target in assembly.ref_map.targets.items()
        if ref in sent_image_refs and target.page_no is not None
    }
    if len(required) < 2:
        return
    planned = {module.get("module_id"): module for module in constraints.get("modules") or []}
    for module in output.modules:
        plan = planned.get(module.module_id)
        if plan is None:
            continue
        source_ids = set(plan.get("source_ids") or ([plan.get("source_id")] if plan.get("source_id") else []))
        page_start = plan.get("page_start")
        page_end = plan.get("page_end")
        relevant = {
            item
            for item in required
            if (not source_ids or item[0] in source_ids)
            and (page_start is None or item[1] >= page_start)
            and (page_end is None or item[1] <= page_end)
        }
        if len(relevant) < 2:
            continue
        got: set[tuple[str, int]] = set()
        for page in module.pages:
            visual = page.visual
            if visual is None or visual.type != "source_asset" or not visual.source_ref:
                continue
            target = assembly.ref_map.get(visual.source_ref)
            if (
                target is not None
                and visual.source_ref in sent_image_refs
                and target.page_no is not None
            ):
                got.add((target.source_id, target.page_no))
        missing = sorted(relevant - got, key=lambda item: (item[0], item[1]))
        if missing:
            pages = "、".join(str(page) for _, page in missing)
            raise ValidationError(
                "module_source_asset_missing",
                f"模块「{module.title or module.module_id}」缺少第 {pages} 页的资料原图学习页；"
                "对照多页图像时，每个物理页都要分别输出 source_asset 学习页",
            )


def _validate_batch(
    db: Session,
    output: ModelOutput,
    payload: dict,
    assembly: Assembly,
) -> None:
    constraints = payload.get("plan_constraints") or {}
    expected = int(payload.get("question_count") or 0)
    sent_image_refs = {
        record["local_ref"]
        for record in assembly.records
        if record.get("sent_as") == "image"
    }
    validate_output(
        output,
        expected_count=expected,
        ref_map=assembly.ref_map,
        has_image=bool(sent_image_refs),
        scope_text=payload.get("scope_text") or "",
        sent_image_refs=sent_image_refs,
        require_pages=True,
        planned_points={
            item.get("display_name")
            for item in constraints.get("items") or []
            if item.get("display_name")
        },
    )
    _require_source_asset_pages(output, constraints, assembly, sent_image_refs, payload)
    point_map = resolve_plan_points(
        db, output, constraints, payload.get("sources") or [], assembly
    )
    delivery = validate_plan_delivery(output, point_map, constraints)
    if delivery.get("shortfall") or delivery.get("extra") or delivery.get("module_shortfall"):
        raise ValidationError(
            "plan_allocation_mismatch",
            "本批考点或模块题量与已确认计划不一致，请按分配重试",
        )
    planned = {module.get("module_id") for module in constraints.get("modules") or []}
    provided = {module.module_id for module in output.modules}
    missing = [module_id for module_id in planned if module_id not in provided]
    if missing:
        raise ValidationError(
            "module_handout_missing",
            "本批缺少模块讲义：" + "、".join(str(item) for item in missing),
        )
    if constraints.get("review_required"):
        reviews = [question for question in output.questions if question.module_id == "review"]
        if not reviews:
            raise ValidationError(
                "module_review_missing",
                "本批缺少综合复习题：请至少把 1 道题改写为 module_id=review 的跨模块应用或对比题，总题量与考点分配不变",
            )


def _run_single(
    db: Session, job: GenerationJob, payload: dict, client
) -> tuple[ModelOutput, Assembly, list[str], dict]:
    assembly = assemble_evidence(db, job)
    _set_stage(db, job, "understand_progress")
    _set_stage(db, job, "write_handout")
    request = build_request(job, assembly)
    request, assembly, fit_stats = _fit_generation_request(request, assembly)
    store_assembly(db, job.id, assembly)
    db.commit()

    def _attempt(current_request, current_assembly, stats):
        if not _job_is_running(db, job.id):
            raise ValidationError("cancelled", "任务已取消")
        result = _call_model(client, current_request)
        request_stats = dict(stats)
        request_stats["job_id"] = job.id
        logger.info("model request sent job=%s %s", job.id, request_stats)
        _set_stage(db, job, "compose_questions")
        output = _parse_output(result, result.finish_reason == "length")
        _set_stage(db, job, "validate")
        sent_image_refs = {
            record["local_ref"]
            for record in current_assembly.records
            if record.get("sent_as") == "image"
        }
        warnings = validate_output(
            output,
            expected_count=int(payload["question_count"]),
            ref_map=current_assembly.ref_map,
            has_image=bool(current_assembly.images),
            scope_text=payload.get("scope_text") or "",
            sent_image_refs=sent_image_refs,
            require_pages=bool(
                (payload.get("plan_constraints") or {}).get("modules_required")
            ),
            planned_points={
                item.get("display_name")
                for item in (payload.get("plan_constraints") or {}).get("items") or []
                if item.get("display_name")
            },
        )
        _require_source_asset_pages(
            output,
            payload.get("plan_constraints") or {},
            current_assembly,
            sent_image_refs,
            payload,
        )
        return output, warnings, dict(result.usage or {})

    try:
        output, warnings, usage = _attempt(request, assembly, fit_stats)
    except ValidationError as exc:
        if exc.code not in {"module_pages_missing", "module_source_asset_missing"}:
            raise
        retry_payload = {
            **payload,
            "retry_note": (
                "上一次输出缺少模块学习页 pages，或缺少对照多页图像所需的资料原图页。"
                "本次必须为每个模块输出有序 pages（至少 1 页），每页包含 page_id、"
                "knowledge_point_ids（本模块考点稳定 ID）、title、explanation、"
                "可选 visual（text/source_asset/comparison/step_sequence）与 related_sources；"
                "若资料含多个物理页的图像且目标要求对照，必须为每个物理页分别输出一页 "
                "source_asset，source_ref 只能使用本次实际发送的图像编号。"
            ),
        }
        retry_request = build_request_for(retry_payload, assembly)
        retry_request, retry_assembly, retry_stats = _fit_generation_request(
            retry_request, assembly
        )
        output, warnings, usage = _attempt(retry_request, retry_assembly, retry_stats)
        assembly = retry_assembly
    return output, assembly, warnings, usage


def _sum_usage(items: list[dict]) -> dict:
    total: dict = {}
    for item in items:
        for key, value in (item or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
            else:
                total[key] = value
    return total


def _run_batched(
    db: Session, job: GenerationJob, payload: dict, client
) -> tuple[ModelOutput, Assembly, list[str], dict]:
    constraints = payload.get("plan_constraints") or {}
    batches = _split_batches(constraints)
    if not batches:
        raise ValidationError("plan_modules_missing", "已确认计划缺少可分批的模块")
    _set_stage(db, job, "understand_progress")
    _set_stage(db, job, "write_handout")
    cache: dict[int, tuple[ModelOutput, Assembly, dict, str, bool]] = {}
    parallel = max(1, min(8, int(settings.generation_max_parallel or 1)))
    if parallel <= 1 or len(batches) <= 1:
        for batch in batches:
            output, assembly, usage, cache_key, cache_hit = _execute_batch(
                db, job, payload, batch, client
            )
            cache[batch["index"]] = (output, assembly, usage, cache_key, cache_hit)
            logger.info(
                "generation batch ready job=%s batch=%s/%s cache_hit=%s",
                job.id,
                batch["index"],
                len(batches),
                cache_hit,
            )
    else:
        semaphore = threading.BoundedSemaphore(parallel)

        def _worker(batch: dict):
            with SessionLocal() as worker_db:
                worker_job = worker_db.get(GenerationJob, job.id)
                if worker_job is None or worker_job.status != "running":
                    raise ValidationError("cancelled", "任务已取消")
                return _execute_batch(
                    worker_db,
                    worker_job,
                    payload,
                    batch,
                    client,
                    model_semaphore=semaphore,
                )

        remaining = deque(batches)
        futures: dict = {}
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            while remaining or futures:
                while remaining and len(futures) < parallel:
                    if not _job_is_running(db, job.id):
                        for future in futures:
                            future.cancel()
                        raise ValidationError("cancelled", "任务已取消")
                    batch = remaining.popleft()
                    futures[pool.submit(_worker, batch)] = batch
                if not futures:
                    break
                done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    batch = futures.pop(future)
                    output, assembly, usage, cache_key, cache_hit = future.result()
                    cache[batch["index"]] = (
                        output,
                        assembly,
                        usage,
                        cache_key,
                        cache_hit,
                    )
                    logger.info(
                        "generation batch ready job=%s batch=%s/%s cache_hit=%s",
                        job.id,
                        batch["index"],
                        len(batches),
                        cache_hit,
                    )
                if remaining and not _job_is_running(db, job.id):
                    for future in futures:
                        future.cancel()
                    raise ValidationError("cancelled", "任务已取消")

    conflict_attempts = 0
    review_attempts = 0
    while True:
        snapshot = [
            (
                index,
                cache[index][0].model_copy(deep=True),
                _copy_assembly(cache[index][1]),
            )
            for index in sorted(cache)
        ]
        try:
            merged, assembly = _merge_batches(snapshot, constraints)
        except BatchConflict as exc:
            conflict_attempts += 1
            if conflict_attempts > 3:
                raise ValidationError(
                    "duplicate_stem",
                    "跨批重复题重跑后仍存在，请调整方案或题量",
                ) from exc
            previous = cache.get(exc.batch_index)
            if previous is not None:
                _delete_batch_cache(db, previous[3])
            batch = next(item for item in batches if item["index"] == exc.batch_index)
            output, assembly, usage, cache_key, cache_hit = _execute_batch(
                db, job, payload, batch, client, force=True
            )
            cache[exc.batch_index] = (output, assembly, usage, cache_key, cache_hit)
            logger.info(
                "generation batch conflict rerun job=%s batch=%s attempt=%s message=%s",
                job.id,
                exc.batch_index,
                conflict_attempts,
                exc.message,
            )
            continue

        point_map = resolve_plan_points(
            db, merged, constraints, payload.get("sources") or [], assembly
        )
        _normalize_review_modules(merged, point_map, constraints)
        try:
            validate_plan_modules(merged, point_map, constraints)
        except ValidationError as exc:
            if exc.code != "module_review_missing":
                raise
            review_attempts += 1
            if review_attempts > 2:
                raise ValidationError(
                    "module_review_missing",
                    "综合复习题生成失败：负责综合复习的批次重跑后仍未生成符合要求的跨模块题目。"
                    "可点击「重试生成」继续，已通过的批次会复用；若仍失败，请返回诊断调整学习方案或减少模块。",
                ) from exc
            target = next(
                (item for item in batches if item["index"] == len(batches)),
                batches[-1],
            )
            previous = cache.get(target["index"])
            if previous is not None:
                _delete_batch_cache(db, previous[3])
            output, assembly, usage, cache_key, cache_hit = _execute_batch(
                db, job, payload, target, client, force=True
            )
            cache[target["index"]] = (output, assembly, usage, cache_key, cache_hit)
            logger.info(
                "generation review batch rerun job=%s batch=%s attempt=%s",
                job.id,
                target["index"],
                review_attempts,
            )
            continue

        delivery = validate_plan_delivery(merged, point_map, constraints)
        if delivery.get("shortfall") or delivery.get("extra") or delivery.get("module_shortfall"):
            raise ValidationError(
                "plan_allocation_mismatch",
                "分批合并后考点或模块题量与已确认计划不一致，请重试",
            )
        break
    if not _job_is_running(db, job.id):
        raise ValidationError("cancelled", "任务已取消")
    store_assembly(db, job.id, assembly)
    db.commit()
    _set_stage(db, job, "validate")
    logger.info(
        "generation batches merged job=%s batches=%s questions=%s",
        job.id,
        len(batches),
        len(merged.questions),
    )
    usages = [cache[batch["index"]][2] for batch in batches]
    return merged, assembly, [], _sum_usage(usages)


def _parse_output(result, output_truncated: bool) -> ModelOutput:
    if output_truncated:
        raise ValidationError("output_truncated", "模型输出被截断，请减少题量或页码范围后重试")
    try:
        data = json.loads(result.content_text)
    except json.JSONDecodeError as exc:
        raise ValidationError("output_truncated", "模型返回的内容不是合法 JSON") from exc
    try:
        return ModelOutput.model_validate(data)
    except PydanticValidationError as exc:
        raise ValidationError("invalid_structure", f"模型输出结构不符合契约：{exc.errors()[:2]}") from exc


def _valid_image_box(box) -> bool:
    return (
        isinstance(box, list)
        and len(box) == 4
        and all(isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1 for value in box)
        and box[2] > box[0]
        and box[3] > box[1]
    )


def _citation_locator(target: RefTarget, citation) -> str:
    locator = json.loads(target.locator_json or "{}")
    if target.kind not in {"page_image", "image_region"}:
        return json.dumps(locator, ensure_ascii=False)
    description = (citation.image_description or "").strip()
    if description:
        locator["image_description"] = description[:300]
    box = citation.box_2d
    if not _valid_image_box(box):
        return json.dumps(locator, ensure_ascii=False)
    parent = [0.0, 0.0, 1.0, 1.0]
    if target.kind == "image_region":
        existing = locator.get("boxes") or ([locator["rect"]] if _valid_image_box(locator.get("rect")) else [])
        if existing and _valid_image_box(existing[0]):
            parent = existing[0]
    if not (
        box[0] >= parent[0]
        and box[1] >= parent[1]
        and box[2] <= parent[2]
        and box[3] <= parent[3]
    ):
        return json.dumps(locator, ensure_ascii=False)
    locator["box_2d"] = list(box)
    locator["boxes"] = [list(box)]
    return json.dumps(locator, ensure_ascii=False)


def _question_source(item, assembly: Assembly, payload: dict) -> str | None:
    for citation in item.citations:
        target = assembly.ref_map.get(citation.ref)
        if target is not None and target.source_id:
            return target.source_id
    specs = payload.get("sources") or []
    return specs[0]["source_id"] if specs else None


def _focus_candidates(payload: dict) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for item in payload.get("focus_points") or []:
        point_id = item.get("knowledge_point_id")
        name = item.get("display_name") or ""
        if point_id and name:
            candidates.append((normalize_point_name(name), point_id))
    return candidates


def _resolve_point(
    db: Session,
    source_id: str | None,
    display_name: str,
    candidates: list[tuple[str, str]],
) -> tuple[str | None, str]:
    text = (display_name or "").strip()
    if not source_id or not text or is_generic_point(text):
        return None, "pending"
    normalized = normalize_point_name(text)
    for candidate_name, point_id in candidates:
        point = db.get(KnowledgePoint, point_id)
        if point is None or point.source_id != source_id:
            continue
        if normalized == candidate_name:
            return point.id, "confirmed"
    point = ensure_knowledge_point(db, source_id, text)
    return point.id, "confirmed"


def resolve_plan_points(
    db: Session,
    output: ModelOutput,
    constraints: dict,
    sources_payload: list[dict],
    assembly: Assembly,
) -> dict[int, str]:
    items = constraints.get("items") or []
    allowed_ids = {
        item.get("knowledge_point_id")
        for item in items
        if item.get("knowledge_point_id")
    }
    by_name: dict[str, list[str]] = {}
    for item in items:
        normalized = normalize_point_name(item.get("display_name") or "")
        point_id = item.get("knowledge_point_id")
        if normalized and point_id:
            by_name.setdefault(normalized, []).append(point_id)
    excluded_names = {
        normalize_point_name(value) for value in constraints.get("excluded_names") or []
    }
    excluded_ids = set(constraints.get("excluded") or [])
    mapping: dict[int, str] = {}
    for question in output.questions:
        raw_id = (question.knowledge_point_id or "").strip()
        normalized = normalize_point_name(question.knowledge_point)
        if raw_id:
            if raw_id in excluded_ids:
                raise ValidationError("plan_excluded_point", "模型为排除考点生成了题目，请重试")
            if raw_id not in allowed_ids:
                raise ValidationError(
                    "plan_point_mismatch",
                    f"第 {question.ordinal} 题返回的考点 ID 不在本批已确认计划中（{raw_id}），请按同一计划重试",
                )
            mapping[question.ordinal] = raw_id
            continue
        if normalized in excluded_names:
            raise ValidationError("plan_excluded_point", "模型为排除考点生成了题目，请重试")
        ids = list(dict.fromkeys(by_name.get(normalized) or []))
        if len(ids) == 1:
            point_id = ids[0]
            if point_id in excluded_ids:
                raise ValidationError("plan_excluded_point", "模型为排除考点生成了题目，请重试")
            mapping[question.ordinal] = point_id
            continue
        if len(ids) > 1:
            raise ValidationError(
                "plan_point_ambiguous",
                "原计划无法区分部分考点，需重新制定学习计划",
            )
        raise ValidationError(
            "plan_point_mismatch",
            "题目考点与已确认计划的分配不一致，请重试",
        )
    return mapping


def validate_plan_delivery(output: ModelOutput, point_map: dict[int, str], constraints: dict) -> dict:
    items = constraints.get("items") or []
    expected_total = constraints.get("total_question_count") or 0
    if expected_total and len(output.questions) != expected_total:
        raise ValidationError(
            "plan_question_count_mismatch",
            f"计划 {expected_total} 题，实际生成 {len(output.questions)} 题，请按同一计划重试",
        )
    expected: dict[str, int] = {}
    for item in items:
        point_id = item.get("knowledge_point_id")
        expected[point_id] = int(item.get("suggested_question_count") or 0)
    actual: dict[str, int] = {}
    for question in output.questions:
        point_id = point_map.get(question.ordinal)
        if point_id is None:
            continue
        actual[point_id] = actual.get(point_id, 0) + 1
    shortfall = []
    extra = []
    for point_id, count in expected.items():
        got = actual.get(point_id, 0)
        if got < count:
            shortfall.append({"knowledge_point_id": point_id, "expected": count, "actual": got, "missing": count - got})
        elif got > count:
            extra.append({"knowledge_point_id": point_id, "expected": count, "actual": got, "extra": got - count})
    modules = constraints.get("modules") or []
    module_of_point = {
        point_id: module.get("module_id")
        for module in modules
        for point_id in module.get("knowledge_point_ids") or []
    }
    expected_by_module = {
        module.get("module_id"): int(module.get("suggested_question_count") or 0)
        for module in modules
    }
    actual_by_module = {module_id: 0 for module_id in expected_by_module}
    for question in output.questions:
        point_id = point_map.get(question.ordinal)
        module_id = module_of_point.get(point_id)
        if module_id in actual_by_module:
            actual_by_module[module_id] += 1
    module_shortfall = [
        {
            "module_id": module_id,
            "title": next(
                (module.get("title") for module in modules if module.get("module_id") == module_id),
                module_id,
            ),
            "expected": count,
            "actual": actual_by_module.get(module_id, 0),
            "missing": count - actual_by_module.get(module_id, 0),
        }
        for module_id, count in expected_by_module.items()
        if actual_by_module.get(module_id, 0) < count
    ]
    return {
        "expected": {"total": expected_total, "by_point": expected, "by_module": expected_by_module},
        "actual": {"total": len(output.questions), "by_point": actual, "by_module": actual_by_module},
        "shortfall": shortfall,
        "extra": extra,
        "module_shortfall": module_shortfall,
        "review_questions": sum(1 for item in output.questions if getattr(item, "module_id", "") == "review"),
        "unplaced_points": constraints.get("unplaced_points") or [],
        "retry_available": True,
        "unconfigured": bool(constraints.get("unconfigured")),
    }


def validate_plan_modules(output: ModelOutput, point_map: dict[int, str], constraints: dict) -> None:
    if not constraints.get("modules_required"):
        return
    modules = constraints.get("modules") or []
    if not modules:
        raise ValidationError("plan_modules_missing", "已确认计划缺少学习模块，请重新生成方案")
    planned = {module.get("module_id"): module for module in modules}
    provided = {module.module_id: module for module in output.modules}
    missing = [module_id for module_id in planned if module_id not in provided]
    if missing:
        raise ValidationError(
            "module_handout_missing",
            "以下模块缺少讲义，不能只交付目录：" + "、".join(missing),
        )
    counts = {module_id: 0 for module_id in planned}
    review_count = 0
    for question in output.questions:
        module_id = question.module_id
        if not module_id:
            raise ValidationError(
                "module_question_unlabeled",
                f"第 {question.ordinal} 题没有模块标识，无法归入学习路线",
            )
        if module_id == "review":
            review_count += 1
            continue
        module = planned.get(module_id)
        if module is None:
            raise ValidationError(
                "module_unknown",
                f"第 {question.ordinal} 题引用了计划之外的模块 {module_id}",
            )
        point_id = point_map.get(question.ordinal)
        if point_id not in (module.get("knowledge_point_ids") or []):
            raise ValidationError(
                "module_point_mismatch",
                f"第 {question.ordinal} 题的考点不属于模块「{module.get('title')}」",
            )
        counts[module_id] += 1
    for module_id, module in planned.items():
        if int(module.get("suggested_question_count") or 0) > 0 and counts[module_id] == 0:
            raise ValidationError(
                "module_practice_missing",
                f"模块「{module.get('title')}」缺少对应练习",
            )
    if len(planned) >= 2 and review_count == 0:
        raise ValidationError(
            "module_review_missing",
            "多模块方案缺少综合复习题，请加入至少 1 道跨模块应用或对比题",
        )


def _recap_body(recap) -> str:
    lines: list[str] = []
    if recap.summary.strip():
        lines.append(recap.summary.strip())
    for rule in recap.rules:
        parts = [part.strip() for part in (rule.condition, rule.result, rule.pitfall) if part.strip()]
        if parts:
            lines.append(" → ".join(parts))
    if recap.cross_module.strip():
        lines.append(recap.cross_module.strip())
    return "\n".join(lines) or "本模块回顾"


def _persist_page_citations(
    db: Session,
    pack_id: str,
    section: HandoutSection,
    page,
    assembly: Assembly,
) -> str | None:
    visual = page.visual
    requests: list[tuple[str, CitationRef]] = []
    source_ref = (visual.source_ref or "").strip() if visual else ""
    if source_ref:
        requests.append(
            (
                source_ref,
                CitationRef(
                    ref=source_ref,
                    claim=(visual.caption or visual.alt_text or page.title or "资料原图"),
                    image_description=(visual.alt_text or "").strip() or None,
                ),
            )
        )
    for citation in page.related_sources:
        requests.append((citation.ref, citation))
    source_citation_id: str | None = None
    created: dict[str, str] = {}
    for ref, citation in requests:
        if ref in created:
            if ref == source_ref:
                source_citation_id = created[ref]
            continue
        target = assembly.ref_map.get(ref)
        if target is None:
            continue
        row = Citation(
            id=new_id("citation"),
            pack_id=pack_id,
            owner_type="section",
            owner_id=section.id,
            evidence_id=target.evidence_id,
            source_id=target.source_id,
            page_no=target.page_no,
            locator_json=_citation_locator(target, citation),
            quote_snapshot=(target.quote_text or "")[:200],
            claim_text=citation.claim or page.title or "",
            local_ref=ref,
            created_at=now_iso(),
        )
        db.add(row)
        db.flush()
        created[ref] = row.id
        if ref == source_ref:
            source_citation_id = row.id
    return source_citation_id


def persist_pack(
    db: Session,
    job: GenerationJob,
    output: ModelOutput,
    assembly: Assembly,
    pack_id: str | None = None,
    point_map: dict[int, str] | None = None,
    delivery: dict | None = None,
) -> str:
    payload = json.loads(job.request_json)
    pack_id = pack_id or new_id("pack")
    progress = output.progress_reading
    title = (payload.get("progress_text") or "学习计划").strip().replace("\n", " ")[:28]
    pack = LearningPack(
        id=pack_id,
        display_title=f"{title}…" if len(title) == 28 else (title or "学习计划"),
        source_kind="real",
        difficulty=payload["difficulty"],
        question_count=int(payload["question_count"]),
        parent_pack_id=payload.get("parent_pack_id"),
        parent_label=payload.get("parent_label"),
        job_id=job.id,
        handout_word_count=0,
        model_name=settings.model,
        prompt_version=PROMPT_VERSION,
        created_at=now_iso(),
    )
    db.add(pack)
    db.flush()

    if payload.get("plan_version_id"):
        db.add(
            PackPlanVersion(
                pack_id=pack_id,
                plan_version_id=payload["plan_version_id"],
                adopted_at=now_iso(),
                delivery_json=json.dumps(delivery, ensure_ascii=False) if delivery else None,
            )
        )

    for spec in payload["sources"]:
        source = db.get(SourceMaterial, spec["source_id"])
        db.add(
            PackSource(
                pack_id=pack_id,
                source_id=source.id,
                page_start=spec.get("page_start"),
                page_end=spec.get("page_end"),
                page_count_used=(
                    spec["page_end"] - spec["page_start"] + 1
                    if spec.get("page_start") and spec.get("page_end")
                    else source.page_count
                ),
                source_name_snapshot=source.display_name,
                source_sha256_snapshot=source.sha256,
                source_kind_snapshot=source.kind,
            )
        )

    db.add(
        ProgressSnapshot(
            pack_id=pack_id,
            raw_text=payload.get("progress_text") or "",
            difficulty=payload["difficulty"],
            question_count=int(payload["question_count"]),
            learned_json=json.dumps(progress.learned, ensure_ascii=False),
            unlearned_json=json.dumps(progress.unlearned, ensure_ascii=False),
            weak_points_json=json.dumps(progress.weak_points, ensure_ascii=False),
            intent_json=json.dumps(progress.intent, ensure_ascii=False),
            insufficient=progress.insufficient,
            conflict_notes_json="[]",
            review_focus_json=json.dumps(payload.get("review_focus") or [], ensure_ascii=False),
            created_at=now_iso(),
        )
    )

    total_words = 0

    def add_section(
        kind: str,
        ordinal: int,
        heading: str,
        body: str,
        content: dict | None = None,
        module_id: str | None = None,
    ) -> HandoutSection:
        nonlocal total_words
        section = HandoutSection(
            id=new_id("section"),
            pack_id=pack_id,
            section_type=kind,
            ordinal=ordinal,
            heading=heading,
            body_text=body,
            content_json=json.dumps(content, ensure_ascii=False) if content else None,
            section_role="neutral",
            is_extension=False,
            word_count=word_count(body),
            module_id=module_id,
        )
        db.add(section)
        db.flush()
        total_words += section.word_count
        return section

    scope_body = "本次覆盖：" + "；".join(output.handout.scope.covered) + "\n本次不覆盖：" + "；".join(output.handout.scope.not_covered)
    add_section(
        "scope",
        1,
        "学习范围",
        scope_body,
        {"covered": output.handout.scope.covered, "not_covered": output.handout.scope.not_covered},
    )

    section_rows: list[tuple[HandoutSection, list]] = []
    ordinal = 1
    for block in output.handout.key_points:
        ordinal += 1
        section_rows.append((add_section("key_point", ordinal, block.heading or "知识要点", block.body), block.citations))
    for block in output.handout.pitfalls:
        ordinal += 1
        section_rows.append((add_section("pitfall", ordinal, block.heading or "易错处", block.body), block.citations))
    for block in output.handout.visual:
        ordinal += 1
        section_rows.append((add_section("visual", ordinal, block.heading or "图文解释", block.body), block.citations))

    plan_modules_by_id = {
        item.get("module_id"): item
        for item in (payload.get("plan_constraints") or {}).get("modules") or []
    }
    for module in output.modules:
        meta = {
            "module_id": module.module_id,
            "title": module.title,
            "objective": module.objective,
            "core_points": module.core_points,
            "completion_standard": module.completion,
        }

        for block in module.key_points:
            ordinal += 1
            section_rows.append(
                (
                    add_section(
                        "module_key_point",
                        ordinal,
                        block.heading or f"{module.title or module.module_id} · 知识要点",
                        block.body,
                        meta,
                        module.module_id,
                    ),
                    block.citations,
                )
            )
        for block in module.pitfalls:
            ordinal += 1
            section_rows.append(
                (
                    add_section(
                        "module_pitfall",
                        ordinal,
                        block.heading or f"{module.title or module.module_id} · 易错处",
                        block.body,
                        meta,
                        module.module_id,
                    ),
                    block.citations,
                )
            )
        for block in module.visual:
            ordinal += 1
            section_rows.append(
                (
                    add_section(
                        "module_visual",
                        ordinal,
                        block.heading or f"{module.title or module.module_id} · 图文解释",
                        block.body,
                        meta,
                        module.module_id,
                    ),
                    block.citations,
                )
            )

        allowed_points = set(
            (
                plan_modules_by_id.get(module.module_id) or {}
            ).get("knowledge_point_ids") or []
        )
        for page_index, page in enumerate(module.pages):
            ordinal += 1
            point_ids = [point_id for point_id in page.knowledge_point_ids if point_id]
            if allowed_points:
                point_ids = [point_id for point_id in point_ids if point_id in allowed_points]
            visual = page.visual.model_dump() if page.visual else None
            content = {
                "page_id": page.page_id or f"page_{module.module_id}_{page_index + 1}",
                "knowledge_point_ids": point_ids,
                "page_type": (visual or {}).get("type") or "text",
                "visual": visual,
            }
            page_section = add_section(
                "module_page",
                ordinal,
                page.title,
                page.explanation,
                content,
                module.module_id,
            )
            if visual is not None and visual.get("source_ref"):
                visual["citation_id"] = _persist_page_citations(
                    db, pack_id, page_section, page, assembly
                )
                page_section.content_json = json.dumps(content, ensure_ascii=False)
            else:
                _persist_page_citations(db, pack_id, page_section, page, assembly)

        if module.recap is not None:
            ordinal += 1
            add_section(
                "module_recap",
                ordinal,
                "模块回顾",
                _recap_body(module.recap),
                module.recap.model_dump(),
                module.module_id,
            )

    for section, citations in section_rows:
        for citation in citations:
            target = assembly.ref_map.get(citation.ref)
            if target is None:
                raise ValidationError("citation_not_found", f"引用 {citation.ref} 不存在")
            db.add(
                Citation(
                    id=new_id("citation"),
                    pack_id=pack_id,
                    owner_type="section",
                    owner_id=section.id,
                    evidence_id=target.evidence_id,
                    source_id=target.source_id,
                    page_no=target.page_no,
                    locator_json=_citation_locator(target, citation),
                    quote_snapshot=(target.quote_text or "")[:200],
                    claim_text=citation.claim or section.heading or "",
                    local_ref=citation.ref,
                    created_at=now_iso(),
                )
            )

    focus_candidates = _focus_candidates(payload)
    plan_display_names = {
        item.get("knowledge_point_id"): item.get("display_name")
        for item in (payload.get("plan_constraints") or {}).get("items") or []
    }
    for item in output.questions:
        source_id = _question_source(item, assembly, payload)
        point_id, reliability = _resolve_point(db, source_id, item.knowledge_point, focus_candidates)
        display_name = item.knowledge_point
        if point_map and item.ordinal in point_map:
            point_id = point_map[item.ordinal]
            reliability = "confirmed"
            display_name = plan_display_names.get(point_id) or display_name
        origin_question_id = None
        variant_kind = "original"
        if payload.get("parent_pack_id"):
            variant_kind = "retry_new"
            for focus in payload.get("focus_points") or []:
                if focus.get("knowledge_point_id") == point_id and focus.get("source_question_id"):
                    origin_question_id = focus["source_question_id"]
                    break
        question = Question(
            id=new_id("question"),
            pack_id=pack_id,
            ordinal=item.ordinal,
            stem=item.stem,
            options_json=json.dumps(
                [{"key": chr(65 + i), "text": text} for i, text in enumerate(item.options)],
                ensure_ascii=False,
            ),
            answer_index=item.answer_index,
            explanation=item.explanation,
            knowledge_point=display_name or "未命名考点",
            module_id=item.module_id or None,
            origin_question_id=origin_question_id,
            variant_kind=variant_kind,
            difficulty_tag=item.difficulty_tag,
            is_image_based=item.is_image_based,
            stem_hash=stem_hash(item.stem),
            created_at=now_iso(),
        )
        db.add(question)
        db.flush()
        point_link = None
        if point_id:
            point_link = QuestionKnowledgePoint(
                question_id=question.id,
                knowledge_point_id=point_id,
                role="primary",
                reliability=reliability,
                created_at=now_iso(),
            )
            db.add(point_link)
        for citation in item.citations:
            target = assembly.ref_map.get(citation.ref)
            if target is None:
                raise ValidationError("citation_not_found", f"引用 {citation.ref} 不存在")
            citation_row = Citation(
                id=new_id("citation"),
                pack_id=pack_id,
                owner_type="question",
                owner_id=question.id,
                evidence_id=target.evidence_id,
                source_id=target.source_id,
                page_no=target.page_no,
                locator_json=_citation_locator(target, citation),
                quote_snapshot=(target.quote_text or "")[:200],
                claim_text=citation.claim or item.knowledge_point or "",
                local_ref=citation.ref,
                created_at=now_iso(),
            )
            db.add(citation_row)
            if point_link is not None and point_link.citation_id is None:
                point_link.citation_id = citation_row.id

    pack.handout_word_count = total_words
    job.pack_id = pack_id
    return pack_id


def ensure_sources_active(db: Session, payload: dict) -> None:
    for spec in payload.get("sources", []):
        source = db.get(SourceMaterial, spec.get("source_id"))
        if source is None or source.status != "active":
            raise ValidationError(
                "source_deleted", "生成期间原资料已被删除，需重新选择或上传，不生成学习计划"
            )


def ensure_no_cross_round_duplicates(db: Session, payload: dict, output: ModelOutput) -> None:
    parent_id = payload.get("parent_pack_id")
    if not parent_id:
        return
    hashes = set(
        db.execute(select(Question.stem_hash).where(Question.pack_id == parent_id)).scalars().all()
    )
    duplicate = [item.ordinal for item in output.questions if stem_hash(item.stem) in hashes]
    if duplicate:
        raise ValidationError(
            "insufficient_evidence",
            json.dumps(
                {
                    "supportable_question_count": max(0, len(output.questions) - len(duplicate)),
                    "reason": "部分题目与上一轮题干重复，需更换考点或补充资料",
                    "suggested_pages": "",
                },
                ensure_ascii=False,
            ),
        )


def _call_model(client, request: ModelRequest) -> ModelResult:
    budget = max(1, min(settings.generate_timeout, 180))
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(client.generate, request)
    try:
        return future.result(timeout=budget)
    except FutureTimeout as exc:
        raise ModelError(
            "generation_timeout", "生成超时，资料与进度已保留，可缩小页码或减少题量后重试"
        ) from exc
    finally:
        pool.shutdown(wait=False)


def _set_stage(db: Session, job: GenerationJob, stage: str) -> None:
    db.execute(
        update(GenerationJob)
        .where(GenerationJob.id == job.id, GenerationJob.status == "running")
        .values(stage=stage, stage_updated_at=now_iso(), updated_at=now_iso())
    )
    db.commit()


def _job_is_running(db: Session, job_id: str) -> bool:
    status = db.execute(
        select(GenerationJob.status).where(GenerationJob.id == job_id)
    ).scalar_one_or_none()
    return status == "running"


def run_job(job_id: str) -> None:
    try:
        claimed = mark_job_running(job_id)
    except Exception:  # noqa: BLE001
        _release_launch(job_id)
        logger.warning("job launch claim failed job_id=%s", job_id, exc_info=True)
        return
    if not claimed:
        return
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        if job is None:
            return
        db.refresh(job)
        try:
            payload = json.loads(job.request_json)
            constraints = payload.get("plan_constraints") or {}
            client = get_model_client()
            if _should_batch(constraints):
                output, assembly, warnings, usage = _run_batched(db, job, payload, client)
            else:
                output, assembly, warnings, usage = _run_single(db, job, payload, client)
            ensure_sources_active(db, payload)
            ensure_no_cross_round_duplicates(db, payload, output)
            constraints = payload.get("plan_constraints") or {}
            point_map = None
            delivery = None
            if constraints:
                if constraints.get("items"):
                    point_map = resolve_plan_points(
                        db, output, constraints, payload.get("sources") or [], assembly
                    )
                    validate_plan_modules(output, point_map, constraints)
                    delivery = validate_plan_delivery(output, point_map, constraints)
                else:
                    delivery = {
                        "expected": {"total": int(payload["question_count"]), "by_point": {}},
                        "actual": {"total": len(output.questions), "by_point": {}},
                        "shortfall": [],
                        "extra": [],
                        "unplaced_points": constraints.get("unplaced_points") or [],
                        "retry_available": True,
                        "unconfigured": True,
                    }
            _set_stage(db, job, "persist")

            pack_id = new_id("pack")
            warning_text = ("content_warning: " + "；".join(warnings[:5])) if warnings else None
            claimed = db.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job.id,
                    GenerationJob.status == "running",
                )
                .values(
                    status="succeeded",
                    pack_id=pack_id,
                    token_usage_json=json.dumps(usage, ensure_ascii=False),
                    error_detail=warning_text,
                    updated_at=now_iso(),
                )
            )
            if claimed.rowcount != 1:
                db.rollback()
                logger.info("job cancelled before publish job_id=%s", job.id)
                return
            persist_pack(
                db,
                job,
                output,
                assembly,
                pack_id=pack_id,
                point_map=point_map,
                delivery=delivery,
            )
            db.commit()
            logger.info("job succeeded job_id=%s pack_id=%s", job.id, pack_id)
        except ModelError as exc:
            db.rollback()
            job = db.get(GenerationJob, job_id)
            _fail(db, job, exc.code, exc.message)
        except ValidationError as exc:
            db.rollback()
            job = db.get(GenerationJob, job_id)
            _fail(db, job, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            job = db.get(GenerationJob, job_id)
            logger.exception("job failed job_id=%s", job_id)
            _fail(db, job, "persist_error", f"生成过程出现异常：{type(exc).__name__}")


def _fail(db: Session, job: GenerationJob | None, code: str, message: str) -> None:
    if job is None:
        return
    failed_status = "insufficient_evidence" if code == "insufficient_evidence" else "failed"
    db.execute(
        update(GenerationJob)
        .where(
            GenerationJob.id == job.id,
            GenerationJob.status.in_(("queued", "running")),
        )
        .values(
            status=failed_status,
            error_code=code,
            error_detail=message,
            updated_at=now_iso(),
        )
    )
    db.commit()


def start_job(job_id: str) -> None:
    thread = threading.Thread(target=run_job, args=(job_id,), name=f"job-{job_id[:12]}", daemon=True)
    thread.start()


def _release_launch(job_id: str) -> None:
    with SessionLocal() as db:
        db.execute(
            update(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.status == "queued",
                GenerationJob.launch_state == "dispatching",
            )
            .values(launch_state="pending_dispatch", updated_at=now_iso())
        )
        db.commit()


def mark_job_running(job_id: str) -> bool:
    with SessionLocal() as db:
        claimed = db.execute(
            update(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.status == "queued",
            )
            .values(
                status="running",
                launch_state="dispatched",
                stage="fetch_sources",
                stage_updated_at=now_iso(),
                error_code=None,
                error_detail=None,
                updated_at=now_iso(),
            )
        )
        db.commit()
        return claimed.rowcount == 1


def create_job(db: Session, payload: dict) -> tuple[GenerationJob, bool]:
    existing = db.execute(
        select(GenerationJob).where(GenerationJob.idempotency_key == payload["idempotency_key"])
    ).scalars().first()
    if existing is not None:
        return existing, True
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    job = GenerationJob(
        id=new_id("job"),
        idempotency_key=payload["idempotency_key"],
        status="queued",
        stage="fetch_sources",
        stage_updated_at=now_iso(),
        request_json=json.dumps(payload, ensure_ascii=False),
        input_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        created_at=now_iso(),
        updated_at=now_iso(),
        launch_state="pending_dispatch",
    )
    db.add(job)
    db.commit()
    return job, False


def create_review_focus(db: Session, pack_id: str, attempt_id: str, wrong_questions: list[Question]) -> None:
    for index, question in enumerate(wrong_questions, start=1):
        point_link = db.execute(
            select(QuestionKnowledgePoint).where(
                QuestionKnowledgePoint.question_id == question.id,
                QuestionKnowledgePoint.role == "primary",
            )
        ).scalars().first()
        db.add(
            ReviewFocus(
                id=new_id("review"),
                source_pack_id=pack_id,
                source_attempt_id=attempt_id,
                question_id=question.id,
                knowledge_point=question.knowledge_point,
                knowledge_point_id=point_link.knowledge_point_id if point_link is not None else None,
                suggestion=f"复习「{question.knowledge_point}」的判断依据",
                edited_text=None,
                ordinal=index,
                is_deleted=False,
            )
        )