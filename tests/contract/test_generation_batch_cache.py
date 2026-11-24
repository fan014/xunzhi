from __future__ import annotations

import json
import time

from sqlalchemy import select, update

from app.domain.generation import _run_batched, create_job, mark_job_running
from app.domain.models import GenerationBatchCache
from app.infra.db import SessionLocal
from app.infra.model_client import FakeModelClient
from tests import stage4_helpers as helpers


class CountingFake(FakeModelClient):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return super().generate(request)


def _constraints(source_id: str, suffix: str = "") -> dict:
    items = []
    modules = []
    for module_no in range(1, 4):
        point_ids = []
        names = []
        module_id = f"mod_{module_no}{suffix}"
        for index in range(2):
            point_id = f"kp_{module_no}_{index}{suffix}"
            name = f"考点{module_no}{index}{suffix}"
            point_ids.append(point_id)
            names.append(name)
            items.append(
                {
                    "knowledge_point_id": point_id,
                    "display_name": name,
                    "module_id": module_id,
                    "module_title": f"模块{module_no}",
                    "suggested_question_count": 2,
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
                "suggested_question_count": 4,
                "completion_standard": "完成本模块练习。",
                "point_allocation": [
                    {
                        "knowledge_point_id": point_id,
                        "display_name": name,
                        "suggested_question_count": 2,
                    }
                    for point_id, name in zip(point_ids, names)
                ],
            }
        )
    return {
        "modules_required": True,
        "total_question_count": 12,
        "items": items,
        "modules": modules,
        "excluded": [],
        "excluded_names": [],
        "unplaced_points": [],
    }


def _payload(source_id: str, constraints: dict, plan_version: str) -> dict:
    return {
        "idempotency_key": f"cache-test-{time.time_ns()}",
        "sources": [{"source_id": source_id, "page_start": None, "page_end": None}],
        "progress_text": "批次缓存测试",
        "difficulty": "consolidate",
        "question_count": 12,
        "consent_ack": True,
        "parent_pack_id": None,
        "review_focus": [],
        "focus_points": [],
        "plan_version_id": plan_version,
        "plan_constraints": constraints,
        "scope_text": "cache_source.txt",
    }


def _run(db, payload: dict, client) -> tuple:
    job, _ = create_job(db, payload)
    db.commit()
    assert mark_job_running(job.id)
    db.refresh(job)
    return _run_batched(db, job, payload, client)


def test_successful_batches_are_reused_across_jobs(client, tmp_path):
    note = tmp_path / "cache_source.txt"
    note.write_text("第一段：TCP 可靠传输。\n\n第二段：路由基础。\n\n第三段：网络安全。", encoding="utf-8")
    source = helpers.upload(client, note)
    plan_version = f"pv_cache_{time.time_ns()}"
    constraints = _constraints(source["id"])
    payload = _payload(source["id"], constraints, plan_version)

    with SessionLocal() as db:
        first = CountingFake()
        output1, _, _, _ = _run(db, payload, first)
        assert len(output1.questions) == 12
        rows = db.execute(
            select(GenerationBatchCache).where(
                GenerationBatchCache.plan_version_id == plan_version
            )
        ).scalars().all()
        assert len(rows) == 3
        assert first.calls == 3

        second_payload = _payload(source["id"], constraints, plan_version)
        second = CountingFake()
        output2, _, _, _ = _run(db, second_payload, second)
        assert len(output2.questions) == 12
        assert second.calls == 0


def test_corrupt_cache_only_reruns_affected_batch(client, tmp_path):
    note = tmp_path / "corrupt_source.txt"
    note.write_text("第一段：TCP 可靠传输。\n\n第二段：路由基础。\n\n第三段：网络安全。", encoding="utf-8")
    source = helpers.upload(client, note)
    plan_version = f"pv_corrupt_{time.time_ns()}"
    constraints = _constraints(source["id"])
    payload = _payload(source["id"], constraints, plan_version)

    with SessionLocal() as db:
        _run(db, payload, CountingFake())
        db.execute(
            update(GenerationBatchCache)
            .where(
                GenerationBatchCache.plan_version_id == plan_version,
                GenerationBatchCache.batch_index == 1,
            )
            .values(batch_json="{broken")
        )
        db.commit()

        second = CountingFake()
        output, _, _, _ = _run(db, _payload(source["id"], constraints, plan_version), second)
        assert len(output.questions) == 12
        assert second.calls == 1


def test_plan_change_invalidates_cached_batches(client, tmp_path):
    note = tmp_path / "changed_source.txt"
    note.write_text("第一段：TCP 可靠传输。\n\n第二段：路由基础。\n\n第三段：网络安全。", encoding="utf-8")
    source = helpers.upload(client, note)
    plan_version = f"pv_changed_{time.time_ns()}"
    payload = _payload(source["id"], _constraints(source["id"]), plan_version)

    with SessionLocal() as db:
        _run(db, payload, CountingFake())
        changed = _constraints(source["id"], suffix="_v2")
        changed_payload = _payload(source["id"], changed, plan_version)
        second = CountingFake()
        output, _, _, _ = _run(db, changed_payload, second)
        assert len(output.questions) == 12
        assert second.calls == 3

def test_cross_batch_duplicate_invalidates_only_later_batch(client, tmp_path):
    note = tmp_path / "duplicate_source.txt"
    note.write_text("第一段：TCP 可靠传输。\n\n第二段：路由基础。\n\n第三段：网络安全。", encoding="utf-8")
    source = helpers.upload(client, note)
    plan_version = f"pv_duplicate_{time.time_ns()}"
    constraints = _constraints(source["id"])
    payload = _payload(source["id"], constraints, plan_version)

    with SessionLocal() as db:
        _run(db, payload, CountingFake())
        rows = db.execute(
            select(GenerationBatchCache)
            .where(GenerationBatchCache.plan_version_id == plan_version)
            .order_by(GenerationBatchCache.batch_index)
        ).scalars().all()
        first = json.loads(rows[0].batch_json)
        second = json.loads(rows[1].batch_json)
        second["questions"][0]["stem"] = first["questions"][0]["stem"]
        db.execute(
            update(GenerationBatchCache)
            .where(GenerationBatchCache.id == rows[1].id)
            .values(batch_json=json.dumps(second, ensure_ascii=False))
        )
        db.commit()

        second_client = CountingFake()
        output, _, _, _ = _run(
            db, _payload(source["id"], constraints, plan_version), second_client
        )
        assert len(output.questions) == 12
        assert len({question.stem for question in output.questions}) == 12
        assert second_client.calls == 1
        remaining = db.execute(
            select(GenerationBatchCache)
            .where(GenerationBatchCache.plan_version_id == plan_version)
        ).scalars().all()
        assert len(remaining) == 3
