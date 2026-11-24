from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import replace

from app.config import settings as base_settings
from app.domain import generation
from app.domain.generation import _run_batched, create_job, mark_job_running
from app.infra.db import SessionLocal
from app.infra.model_client import FakeModelClient
from tests import stage4_helpers as helpers


def _constraints(source_id: str) -> dict:
    items = []
    modules = []
    for module_no in range(1, 4):
        point_id = f"kp_parallel_{module_no}"
        name = f"并行考点{module_no}"
        module_id = f"mod_parallel_{module_no}"
        items.append(
            {
                "knowledge_point_id": point_id,
                "display_name": name,
                "module_id": module_id,
                "module_title": f"并行模块{module_no}",
                "suggested_question_count": 4,
                "priority": "pending",
            }
        )
        modules.append(
            {
                "module_id": module_id,
                "title": f"并行模块{module_no}",
                "objective": f"掌握{name}",
                "core_points": [name],
                "knowledge_point_ids": [point_id],
                "source_ids": [source_id],
                "suggested_question_count": 4,
                "completion_standard": "完成本模块练习。",
                "point_allocation": [
                    {
                        "knowledge_point_id": point_id,
                        "display_name": name,
                        "suggested_question_count": 4,
                    }
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
        "idempotency_key": f"parallel-{time.time_ns()}",
        "sources": [{"source_id": source_id, "page_start": None, "page_end": None}],
        "progress_text": "并行批次测试",
        "difficulty": "consolidate",
        "question_count": 12,
        "consent_ack": True,
        "parent_pack_id": None,
        "review_focus": [],
        "focus_points": [],
        "plan_version_id": plan_version,
        "plan_constraints": constraints,
        "scope_text": "parallel.txt",
    }


class DelayedFake(FakeModelClient):
    def __init__(self, delays: dict[str, float]):
        super().__init__()
        self.delays = delays
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.intervals: list[tuple[float, float, str]] = []

    def _module(self, request) -> str:
        marker = "【模块计划】\n"
        raw = request.task_text.split(marker, 1)[1] if marker in request.task_text else "[]"
        modules, _ = json.JSONDecoder().raw_decode(raw.strip())
        return modules[0].get("module_id", "") if modules else ""

    def generate(self, request):
        module_id = self._module(request)
        delay = self.delays.get(module_id, 0.02)
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            started = time.perf_counter()
        try:
            time.sleep(delay)
            return super().generate(request)
        finally:
            with self.lock:
                self.active -= 1
                self.intervals.append((started, time.perf_counter(), module_id))


def _run(client, model, parallel: int, tmp_path, monkeypatch) -> tuple:
    monkeypatch.setattr(
        generation,
        "settings",
        replace(base_settings, generation_max_parallel=parallel),
    )
    note = tmp_path / "parallel.txt"
    note.write_text("第一段：TCP 可靠传输。\n\n第二段：路由基础。\n\n第三段：网络安全。", encoding="utf-8")
    source = helpers.upload(client, note)
    constraints = _constraints(source["id"])
    payload = _payload(source["id"], constraints, f"pv_parallel_{parallel}_{time.time_ns()}")
    with SessionLocal() as db:
        job, _ = create_job(db, payload)
        db.commit()
        assert mark_job_running(job.id)
        db.refresh(job)
        output, assembly, _, _ = _run_batched(db, job, payload, model)
    return output, model


def test_parallel_two_overlaps_and_caps_at_two(client, tmp_path, monkeypatch):
    model = DelayedFake({"mod_parallel_1": 0.12, "mod_parallel_2": 0.12, "mod_parallel_3": 0.02})
    output, model = _run(client, model, 2, tmp_path, monkeypatch)
    assert len(output.questions) == 12
    assert model.peak <= 2
    assert model.peak == 2
    overlaps = 0
    intervals = sorted(model.intervals)
    for index, (start, end, _) in enumerate(intervals):
        if index + 1 < len(intervals) and start < intervals[index + 1][0] < end:
            overlaps += 1
    assert overlaps >= 1


def test_parallel_one_has_no_overlap(client, tmp_path, monkeypatch):
    model = DelayedFake({"mod_parallel_1": 0.05, "mod_parallel_2": 0.05, "mod_parallel_3": 0.05})
    output, model = _run(client, model, 1, tmp_path, monkeypatch)
    assert len(output.questions) == 12
    assert model.peak == 1
    intervals = sorted(model.intervals)
    for index in range(len(intervals) - 1):
        assert intervals[index][1] <= intervals[index + 1][0]


def test_out_of_order_completion_keeps_module_order(client, tmp_path, monkeypatch):
    model = DelayedFake({"mod_parallel_1": 0.15, "mod_parallel_2": 0.02, "mod_parallel_3": 0.02})
    output, model = _run(client, model, 2, tmp_path, monkeypatch)
    assert [module.module_id for module in output.modules] == [
        "mod_parallel_1",
        "mod_parallel_2",
        "mod_parallel_3",
    ]
    finished = [module_id for _, _, module_id in sorted(model.intervals, key=lambda item: item[1])]
    assert finished.index("mod_parallel_2") < finished.index("mod_parallel_1")