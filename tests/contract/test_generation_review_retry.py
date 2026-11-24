from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import select, update

from app.domain import generation
from app.domain.knowledge import normalize_point_name
from app.domain.generation import (
    _batch_payload,
    _run_batched,
    _split_batches,
    create_job,
    mark_job_running,
    resolve_plan_points,
    validate_plan_delivery,
    validate_plan_modules,
)
from app.domain.models import (
    GenerationBatchCache,
    KnowledgePoint,
    LearningPack,
    LearningPlan,
    LearningPlanVersion,
    PackPlanVersion,
    now_iso,
)
from app.domain.schemas import ModelOutput
from app.domain.validation import ValidationError
from app.infra.db import SessionLocal
from app.infra.model_client import FakeModelClient
from tests import stage4_helpers as helpers


def _constraints(source_id: str) -> dict:
    items = []
    modules = []
    for module_no in range(1, 4):
        names = [f"考点{module_no}A", f"考点{module_no}B"]
        point_ids = [f"kp_review_{module_no}_{chr(65 + index)}" for index in range(2)]
        module_id = f"mod_review_{module_no}"
        for point_id, name in zip(point_ids, names):
            items.append(
                {
                    "knowledge_point_id": point_id,
                    "display_name": name,
                    "module_id": module_id,
                    "module_title": f"模块{module_no}",
                    "suggested_question_count": 2 if point_id.endswith("A") else 1,
                    "priority": "pending",
                }
            )
        modules.append(
            {
                "module_id": module_id,
                "title": f"模块{module_no}",
                "objective": f"掌握模块{module_no}",
                "core_points": names,
                "knowledge_point_ids": point_ids,
                "source_ids": [source_id],
                "suggested_question_count": 3,
                "completion_standard": "能独立完成本模块练习。",
                "point_allocation": [
                    {
                        "knowledge_point_id": point_ids[0],
                        "display_name": names[0],
                        "suggested_question_count": 2,
                    },
                    {
                        "knowledge_point_id": point_ids[1],
                        "display_name": names[1],
                        "suggested_question_count": 1,
                    },
                ],
            }
        )
    return {
        "modules_required": True,
        "total_question_count": 9,
        "items": items,
        "modules": modules,
        "excluded": [],
        "excluded_names": [],
        "unplaced_points": [],
    }


def _payload(source_id: str, constraints: dict, plan_version: str) -> dict:
    return {
        "idempotency_key": f"review-test-{time.time_ns()}",
        "sources": [{"source_id": source_id, "page_start": None, "page_end": None}],
        "progress_text": "综合复习缺失重试验收",
        "difficulty": "consolidate",
        "question_count": 9,
        "consent_ack": True,
        "parent_pack_id": None,
        "review_focus": [],
        "focus_points": [],
        "plan_version_id": plan_version,
        "plan_constraints": constraints,
        "scope_text": "review_source.txt",
    }


def _run(db, payload: dict, client) -> tuple:
    job, _ = create_job(db, payload)
    db.commit()
    assert mark_job_running(job.id)
    db.refresh(job)
    return job, _run_batched(db, job, payload, client)


def _batch_key(request) -> str:
    marker = "【模块计划】\n"
    raw = request.task_text.split(marker, 1)[1] if marker in request.task_text else "[]"
    try:
        modules, _ = json.JSONDecoder().raw_decode(raw.strip())
    except json.JSONDecodeError:
        modules = []
    return ",".join(module.get("module_id") or "" for module in modules)


def _drop_review_questions(output_text: str) -> str:
    data = json.loads(output_text)
    module_of = {
        point: module.get("module_id")
        for module in data.get("modules") or []
        for point in module.get("core_points") or []
    }
    for question in data.get("questions") or []:
        if question.get("module_id") == "review":
            question["module_id"] = module_of.get(question.get("knowledge_point"), "")
    return json.dumps(data, ensure_ascii=False)


class MissingReviewOnce(FakeModelClient):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.review_calls = 0
        self.calls_per_batch: dict[str, int] = {}
        self.retry_notes: list[str] = []

    def generate(self, request):
        self.calls += 1
        key = _batch_key(request)
        self.calls_per_batch[key] = self.calls_per_batch.get(key, 0) + 1
        if "【本次修正】" in request.task_text:
            self.retry_notes.append(request.task_text)
        result = super().generate(request)
        if "【本批综合复习】" in request.task_text and self.review_calls == 0:
            self.review_calls += 1
            result.content_text = _drop_review_questions(result.content_text)
        return result


class AlwaysMissingReview(FakeModelClient):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.calls_per_batch: dict[str, int] = {}

    def generate(self, request):
        self.calls += 1
        key = _batch_key(request)
        self.calls_per_batch[key] = self.calls_per_batch.get(key, 0) + 1
        result = super().generate(request)
        if "【本批综合复习】" in request.task_text:
            result.content_text = _drop_review_questions(result.content_text)
        return result


class CountingFake(FakeModelClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return super().generate(request)


def _source(client, tmp_path, name: str) -> dict:
    note = tmp_path / name
    note.write_text(
        "第一段：TCP 用序号和确认实现可靠传输。\n\n"
        "第二段：交换机按 MAC 地址表转发帧。\n\n"
        "第三段：路由器按路由表转发跨网段分组。",
        encoding="utf-8",
    )
    return helpers.upload(client, note)


def test_missing_review_in_last_batch_retries_only_that_batch(client, tmp_path):
    source = _source(client, tmp_path, "review_once.txt")
    plan_version = f"pv_review_once_{time.time_ns()}"
    constraints = _constraints(source["id"])
    payload = _payload(source["id"], constraints, plan_version)

    with SessionLocal() as db:
        job, (output, assembly, _, _) = _run(db, payload, MissingReviewOnce())

        rows = db.execute(
            select(GenerationBatchCache)
            .where(GenerationBatchCache.plan_version_id == plan_version)
            .order_by(GenerationBatchCache.batch_index)
        ).scalars().all()
        assert len(rows) == 3
        last = ModelOutput.model_validate_json(rows[-1].batch_json)
        assert any(question.module_id == "review" for question in last.questions)

    assert len(output.questions) == 9
    reviews = [question for question in output.questions if question.module_id == "review"]
    assert len(reviews) == 1
    point_map = resolve_plan_points(None, output, constraints, payload["sources"], assembly)
    delivery = validate_plan_delivery(output, point_map, constraints)
    assert delivery["actual"]["by_module"] == {
        module["module_id"]: module["suggested_question_count"] for module in constraints["modules"]
    }
    validate_plan_modules(output, point_map, constraints)


def test_first_missing_review_output_is_not_cached(client, tmp_path):
    source = _source(client, tmp_path, "review_counts.txt")
    plan_version = f"pv_review_counts_{time.time_ns()}"
    constraints = _constraints(source["id"])
    payload = _payload(source["id"], constraints, plan_version)
    with SessionLocal() as db:
        client_model = MissingReviewOnce()
        _run(db, payload, client_model)
    last_module = constraints["modules"][-1]["module_id"]
    assert client_model.review_calls == 1
    assert client_model.calls_per_batch[last_module] == 2
    assert sum(client_model.calls_per_batch.values()) == 4
    assert len(client_model.retry_notes) == 1
    assert "缺少综合复习题" in client_model.retry_notes[0]
    assert "module_id=review" in client_model.retry_notes[0]


def test_stale_cache_without_review_invalidates_only_review_batch(client, tmp_path):
    source = _source(client, tmp_path, "review_stale.txt")
    plan_version = f"pv_review_stale_{time.time_ns()}"
    constraints = _constraints(source["id"])
    payload = _payload(source["id"], constraints, plan_version)

    with SessionLocal() as db:
        _run(db, payload, CountingFake())
        rows = db.execute(
            select(GenerationBatchCache)
            .where(GenerationBatchCache.plan_version_id == plan_version)
            .order_by(GenerationBatchCache.batch_index)
        ).scalars().all()
        assert len(rows) == 3
        last = rows[-1]
        last_json = json.loads(last.batch_json)
        assert any(question.get("module_id") == "review" for question in last_json["questions"])
        db.execute(
            update(GenerationBatchCache)
            .where(GenerationBatchCache.id == last.id)
            .values(batch_json=_drop_review_questions(last.batch_json))
        )
        db.commit()

        second = CountingFake()
        second_job, (output, assembly, _, _) = _run(
            db,
            _payload(source["id"], constraints, plan_version),
            second,
        )
        assert second.calls == 1
        rows = db.execute(
            select(GenerationBatchCache)
            .where(GenerationBatchCache.plan_version_id == plan_version)
            .order_by(GenerationBatchCache.batch_index)
        ).scalars().all()
        assert len(rows) == 3
        rebuilt = ModelOutput.model_validate_json(rows[-1].batch_json)
        assert any(question.module_id == "review" for question in rebuilt.questions)

    assert len(output.questions) == 9
    assert sum(1 for question in output.questions if question.module_id == "review") == 1
    assert second_job.id


def test_review_missing_exhausts_batch_retries_with_actionable_message(client, tmp_path):
    source = _source(client, tmp_path, "review_missing.txt")
    plan_version = f"pv_review_missing_{time.time_ns()}"
    constraints = _constraints(source["id"])
    payload = _payload(source["id"], constraints, plan_version)

    with SessionLocal() as db:
        with pytest.raises(ValidationError) as info:
            _run(db, payload, AlwaysMissingReview())
    assert info.value.code == "module_review_missing"
    assert "综合复习题生成失败" in info.value.message
    assert "重试生成" in info.value.message

def test_stale_review_cache_publishes_one_formal_pack(client, tmp_path, monkeypatch):
    source = _source(client, tmp_path, "review_publish.txt")
    plan_version = f"pv_review_publish_{time.time_ns()}"
    plan_id = f"plan_review_{time.time_ns()}"
    constraints = _constraints(source["id"])

    with SessionLocal() as db:
        for item in constraints["items"]:
            db.add(
                KnowledgePoint(
                    id=item["knowledge_point_id"],
                    source_id=source["id"],
                    display_name=item["display_name"],
                    name_normalized=normalize_point_name(item["display_name"]),
                    status="active",
                    created_at=now_iso(),
                    updated_at=now_iso(),
                )
            )
        db.add(
            LearningPlan(
                id=plan_id,
                title="综合复习回归计划",
                status="confirmed",
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
        db.add(
            LearningPlanVersion(
                id=plan_version,
                plan_id=plan_id,
                status="confirmed",
                origin="manual",
                sources_json=json.dumps(
                    [{"source_id": source["id"], "page_start": None, "page_end": None}],
                    ensure_ascii=False,
                ),
                scope_json=json.dumps({"auto_plan": True}, ensure_ascii=False),
                items_json=json.dumps(constraints["items"], ensure_ascii=False),
                modules_json=json.dumps(constraints["modules"], ensure_ascii=False),
                question_count=9,
                created_at=now_iso(),
                confirmed_at=now_iso(),
            )
        )
        db.commit()

        _run(db, _payload(source["id"], constraints, plan_version), CountingFake())
        rows = db.execute(
            select(GenerationBatchCache)
            .where(GenerationBatchCache.plan_version_id == plan_version)
            .order_by(GenerationBatchCache.batch_index)
        ).scalars().all()
        assert len(rows) == 3
        db.execute(
            update(GenerationBatchCache)
            .where(GenerationBatchCache.id == rows[-1].id)
            .values(batch_json=_drop_review_questions(rows[-1].batch_json))
        )
        db.commit()

        second_job, _ = create_job(db, _payload(source["id"], constraints, plan_version))
        db.commit()
        second_job_id = second_job.id

    counting = CountingFake()
    monkeypatch.setattr(generation, "get_model_client", lambda: counting)
    generation.run_job(second_job_id)

    with SessionLocal() as db:
        job = db.get(generation.GenerationJob, second_job_id)
        assert job.status == "succeeded", job.error_detail
        assert counting.calls == 1
        packs = db.execute(
            select(LearningPack).where(LearningPack.job_id == second_job_id)
        ).scalars().all()
        assert len(packs) == 1
        assert packs[0].question_count == 9
        links = db.execute(
            select(PackPlanVersion).where(PackPlanVersion.pack_id == packs[0].id)
        ).scalars().all()
        assert len(links) == 1
        assert links[0].plan_version_id == plan_version


def test_review_batch_payload_carries_other_modules_and_sources():
    constraints = _constraints("source_a")
    constraints["modules"][0]["source_ids"] = ["source_a"]
    constraints["modules"][1]["source_ids"] = ["source_b"]
    constraints["modules"][2]["source_ids"] = ["source_a"]
    payload = {
        "idempotency_key": "review-payload-test",
        "sources": [
            {"source_id": "source_a", "page_start": None, "page_end": None},
            {"source_id": "source_b", "page_start": None, "page_end": None},
        ],
        "progress_text": "跨模块综合复习",
        "difficulty": "consolidate",
        "question_count": 9,
        "plan_version_id": "pv_review_payload",
        "plan_constraints": constraints,
        "scope_text": "source_a；source_b",
    }
    batches = _split_batches(constraints)
    assert len(batches) == 3
    first = _batch_payload(payload, batches[0], last=False)
    assert first["plan_constraints"]["review_required"] is False
    assert [spec["source_id"] for spec in first["sources"]] == ["source_a"]

    last = _batch_payload(payload, batches[-1], last=True)
    assert {spec["source_id"] for spec in last["sources"]} == {"source_a", "source_b"}
    sub = last["plan_constraints"]
    assert sub["review_required"] is True
    assert sub["total_question_count"] == 3
    assert {module["module_id"] for module in sub["review_context"]} == {
        "mod_review_1",
        "mod_review_2",
    }
    assert {item["knowledge_point_id"] for item in sub["items"]} == {
        "kp_review_3_A",
        "kp_review_3_B",
    }
