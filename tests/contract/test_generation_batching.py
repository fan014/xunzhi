from __future__ import annotations

import json
import time

from app.domain.generation import (
    _run_batched,
    create_job,
    mark_job_running,
    resolve_plan_points,
    validate_plan_delivery,
    validate_plan_modules,
)
from app.domain.schemas import stem_hash
from app.infra.db import SessionLocal
from app.infra.model_client import FakeModelClient, ModelError
from tests import stage4_helpers as helpers


def _constraints(source_id: str) -> dict:
    items = []
    modules = []
    for module_no in range(1, 13):
        count = 4 if module_no <= 11 else 2
        names = [f"考点{module_no:02d}A"] + ([f"考点{module_no:02d}B"] if module_no <= 11 else [])
        point_ids = [f"kp_{module_no:02d}_{chr(65 + index)}" for index in range(len(names))]
        module_id = f"mod_{module_no:02d}"
        for point_id, name in zip(point_ids, names):
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
                "suggested_question_count": count,
                "completion_standard": "能独立完成本模块练习。",
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
        "total_question_count": 46,
        "items": items,
        "modules": modules,
        "excluded": [],
        "excluded_names": [],
        "unplaced_points": [],
    }


def _payload(source_id: str, constraints: dict) -> dict:
    return {
        "idempotency_key": f"batch-test-{time.time_ns()}",
        "sources": [{"source_id": source_id, "page_start": None, "page_end": None}],
        "progress_text": "分批生成验收",
        "difficulty": "consolidate",
        "question_count": 46,
        "consent_ack": True,
        "parent_pack_id": None,
        "review_focus": [],
        "focus_points": [],
        "plan_version_id": None,
        "plan_constraints": constraints,
        "scope_text": "batch_source.txt",
    }


class FailSecondBatchOnce(FakeModelClient):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.batch_calls: dict[str, int] = {}

    def _key(self, request) -> str:
        marker = "【模块计划】\n"
        raw = request.task_text.split(marker, 1)[1] if marker in request.task_text else "[]"
        try:
            modules, _ = json.JSONDecoder().raw_decode(raw.strip())
        except json.JSONDecodeError:
            modules = []
        return ",".join(module.get("module_id") or "" for module in modules)

    def generate(self, request):
        self.calls += 1
        key = self._key(request)
        self.batch_calls[key] = self.batch_calls.get(key, 0) + 1
        if self.calls == 2:
            raise ModelError("upstream_error", "模拟第 2 批失败")
        return super().generate(request)


def test_batched_generation_delivers_46_questions_and_retries_only_failed_batch(client, tmp_path):
    note = tmp_path / "batch_source.txt"
    note.write_text(
        "第一章 传输层：TCP 用序号和确认实现可靠传输。\n\n"
        "第二章 路由：OSPF 用链路状态计算最短路径。\n\n"
        "第三章 安全：数字签名保证完整性和不可否认。",
        encoding="utf-8",
    )
    source = helpers.upload(client, note)
    constraints = _constraints(source["id"])
    payload = _payload(source["id"], constraints)
    with SessionLocal() as db:
        job, _ = create_job(db, payload)
        db.commit()
        assert mark_job_running(job.id)
        db.refresh(job)
        client_model = FailSecondBatchOnce()
        output, assembly, warnings, usage = _run_batched(db, job, payload, client_model)

    assert len(output.questions) == 46
    assert len({stem_hash(question.stem) for question in output.questions}) == 46
    assert len(output.modules) == 12
    assert len(assembly.ref_map.targets) >= 1
    assert usage["fake"] is True

    point_map = resolve_plan_points(
        None,
        output,
        constraints,
        payload["sources"],
        assembly,
    )

    batches = list(client_model.batch_calls.values())
    assert len(batches) >= 6
    assert all(count in {1, 2} for count in batches)
    assert batches.count(2) == 1
    assert sum(batches) == len(batches) + 1

    reviews = [question for question in output.questions if question.module_id == "review"]
    assert len(reviews) == 1
    delivery = validate_plan_delivery(output, point_map, constraints)
    assert delivery["actual"]["total"] == 46
    assert delivery["actual"]["by_module"] == {
        module["module_id"]: module["suggested_question_count"] for module in constraints["modules"]
    }
    validate_plan_modules(output, point_map, constraints)
