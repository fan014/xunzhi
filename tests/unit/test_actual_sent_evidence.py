from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.domain.generation import (
    Assembly,
    _batch_signature,
    _execute_batch,
    _fit_generation_request,
    _run_single,
    _trim_assembly,
    create_job,
    mark_job_running,
)
from app.domain.schemas import ModelOutput, RefMap, RefTarget
from app.domain.validation import ValidationError, validate_output
from app.config import settings as base_settings
from app.domain import generation
from app.infra.db import SessionLocal
from app.infra.model_client import ImagePayload, ModelRequest, ModelResult, TextPayload, fit_request


def _target(ref: str, kind: str = "text_span") -> RefTarget:
    return RefTarget(
        evidence_id=f"ev_{ref}",
        source_id="src_test",
        kind=kind,
        page_no=1 if kind == "page_image" else None,
        confidence="high",
        locator_json='{"chars":[0,10]}',
        quote_text="证据原文",
        label_text=None,
    )


def _assembly() -> Assembly:
    assembly = Assembly()
    for ref in ("T1", "T2"):
        assembly.ref_map.targets[ref] = _target(ref)
    for ref in ("P1", "P2"):
        assembly.ref_map.targets[ref] = _target(ref, "page_image")
    assembly.text_items = [
        TextPayload(ref="T1", description="[T1] 文本证据一"),
        TextPayload(ref="T2", description="[T2] 文本证据二"),
    ]
    assembly.images = [
        ImagePayload(ref="P1", description="图一", data=b"a" * 500_000, mime="image/png"),
        ImagePayload(ref="P2", description="图二", data=b"b" * 500_000, mime="image/png"),
    ]
    assembly.records = [
        {"local_ref": ref, "evidence_id": f"ev_{ref}", "kind": "text_span", "source_id": "src_test", "sent_as": "text", "ordinal": 1}
        for ref in ("T1", "T2")
    ] + [
        {"local_ref": ref, "evidence_id": f"ev_{ref}", "kind": "page_image", "source_id": "src_test", "sent_as": "image", "ordinal": 3}
        for ref in ("P1", "P2")
    ]
    return assembly


def _request() -> ModelRequest:
    return ModelRequest(
        system_prompt="system",
        task_text="task",
        text_items=[
            TextPayload(ref="T1", description="[T1] 文本证据一"),
            TextPayload(ref="T2", description="[T2] 文本证据二"),
        ],
        images=[
            ImagePayload(ref="P1", description="图一", data=b"a" * 500_000, mime="image/png"),
            ImagePayload(ref="P2", description="图二", data=b"b" * 500_000, mime="image/png"),
        ],
        question_count=2,
    )


def _block(ref: str, words: int = 120) -> dict:
    return {"heading": "要点", "body": "讲义正文" * words, "citations": [{"ref": ref}]}


def _output(ref: str, module_id: str = "mod_1", *, image_based: bool = False) -> ModelOutput:
    return ModelOutput.model_validate(
        {
            "progress_reading": {},
            "handout": {
                "scope": {"covered": ["覆盖"], "not_covered": ["不覆盖"]},
                "key_points": [_block("T1"), _block("T1"), _block("T1")],
                "pitfalls": [_block("T1"), _block("T1")],
                "visual": [_block(ref)],
            },
            "modules": [
                {
                    "module_id": module_id,
                    "title": "模块1",
                    "core_points": ["考点1"],
                    "key_points": [_block("T1"), _block("T1")],
                    "pitfalls": [_block("T1")],
                    "visual": [],
                    "pages": [
                        {
                            "page_id": "p1",
                            "knowledge_point_ids": ["kp_1"],
                            "title": "模块学习页",
                            "explanation": "本页正文",
                            "visual": (
                                {"type": "source_asset", "source_ref": ref}
                                if ref.startswith("P")
                                else None
                            ),
                            "related_sources": [{"ref": ref}],
                        }
                    ],
                    "recap": {
                        "summary": "模块回顾",
                        "rules": [
                            {
                                "condition": "已知条件",
                                "result": "判断结果",
                                "pitfall": "常见误区",
                            }
                        ],
                    },
                }
            ],
            "questions": [
                {
                    "ordinal": 1,
                    "stem": "读图题",
                    "options": ["A", "B", "C", "D"],
                    "answer_index": 0,
                    "explanation": "解析",
                    "knowledge_point": "考点1",
                    "module_id": module_id,
                    "is_image_based": image_based,
                    "citations": [{"ref": ref}],
                },
                {
                    "ordinal": 2,
                    "stem": "文本题",
                    "options": ["A", "B", "C", "D"],
                    "answer_index": 0,
                    "explanation": "解析",
                    "knowledge_point": "考点1",
                    "module_id": module_id,
                    "citations": [{"ref": "T1"}],
                },
            ],
        }
    )


def test_trimmed_request_drops_image_ref_and_rejects_it():
    fitted, fit = fit_request(_request(), max_bytes=700_000)
    assert {item.ref for item in fitted.images} == {"P1"}
    assert fit.dropped_images == 1
    trimmed = _trim_assembly(_assembly(), fitted)
    assert "P2" not in trimmed.ref_map.targets
    assert "P1" in trimmed.ref_map.targets
    assert {
        record["local_ref"] for record in trimmed.records if record["sent_as"] == "image"
    } == {"P1"}

    with pytest.raises(ValidationError) as excinfo:
        validate_output(
            _output("P2", image_based=True),
            2,
            trimmed.ref_map,
            has_image=True,
            sent_image_refs={"P1"},
        )
    assert excinfo.value.code == "citation_not_found"

    warnings = validate_output(
        _output("P1", image_based=True),
        2,
        trimmed.ref_map,
        has_image=True,
        sent_image_refs={"P1"},
    )
    assert isinstance(warnings, list)


def test_trimmed_text_ref_is_not_citable():
    request = ModelRequest(
        system_prompt="system",
        task_text="task",
        text_items=[
            TextPayload(ref="T1", description="证" * 1000),
            TextPayload(ref="T2", description="证" * 1000),
            TextPayload(ref="T3", description="证" * 1000),
        ],
        images=[],
        question_count=2,
    )
    fitted, fit = fit_request(request, max_bytes=5_000)
    assert "T3" not in {item.ref for item in fitted.text_items}
    assert fit.dropped_text_items >= 1
    assembly = Assembly()
    for ref in ("T1", "T2", "T3"):
        assembly.ref_map.targets[ref] = _target(ref)
    trimmed = _trim_assembly(assembly, fitted)
    assert "T3" not in trimmed.ref_map.targets
    with pytest.raises(ValidationError) as excinfo:
        validate_output(
            _output("T3", image_based=False),
            2,
            trimmed.ref_map,
            has_image=False,
        )
    assert excinfo.value.code == "citation_not_found"


def test_batch_cache_fingerprint_changes_with_budget(monkeypatch):
    payload = {
        "plan_version_id": "pv_budget",
        "sources": [],
        "plan_constraints": {
            "modules": [{"module_id": "mod_1", "knowledge_point_ids": ["kp_1"]}],
            "items": [{"knowledge_point_id": "kp_1", "display_name": "考点1"}],
        },
    }
    batch = {"index": 1, "rows": [{"module": {"module_id": "mod_1"}}], "question_count": 2}
    assembly = _assembly()
    monkeypatch.setattr(generation, "settings", replace(base_settings, model_max_images=12))
    first = _batch_signature(None, payload, batch, assembly)
    monkeypatch.setattr(generation, "settings", replace(base_settings, model_max_images=3))
    second = _batch_signature(None, payload, batch, assembly)
    assert first != second


class _CiteClient:
    def __init__(self, ref: str):
        self.ref = ref
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        payload = _output(self.ref, "mod_1", image_based=True).model_dump()
        return ModelResult(
            content_text=json.dumps(payload, ensure_ascii=False),
            finish_reason="stop",
            usage={"fake": True},
            latency_ms=1,
            request_stats={},
        )


def test_batch_path_rejects_dropped_ref_then_accepts_kept_ref(client, monkeypatch, tmp_path):
    monkeypatch.setattr(generation, "settings", replace(base_settings, model_max_request_mb=1))
    monkeypatch.setattr(generation, "assemble_for_sources", lambda *args, **kwargs: _assembly())
    payload = {
        "idempotency_key": f"actual-sent-{tmp_path.name}",
        "sources": [],
        "progress_text": "test",
        "difficulty": "consolidate",
        "question_count": 2,
        "plan_version_id": "pv_actual",
        "plan_constraints": {
            "modules_required": True,
            "total_question_count": 2,
            "items": [
                {
                    "knowledge_point_id": "kp_1",
                    "display_name": "考点1",
                    "module_id": "mod_1",
                    "suggested_question_count": 2,
                }
            ],
            "modules": [
                {
                    "module_id": "mod_1",
                    "title": "模块1",
                    "knowledge_point_ids": ["kp_1"],
                    "suggested_question_count": 2,
                }
            ],
        },
        "scope_text": "test",
    }
    batch = {
        "index": 1,
        "rows": [{"module": payload["plan_constraints"]["modules"][0]}],
        "point_ids": {"kp_1"},
        "question_count": 2,
        "source_ids": [],
    }
    with SessionLocal() as db:
        job, _ = create_job(db, payload)
        db.commit()
        assert mark_job_running(job.id)
        db.refresh(job)
        failing = _CiteClient("P2")
        with pytest.raises(ValidationError) as excinfo:
            _execute_batch(db, job, payload, batch, failing, force=True)
        assert excinfo.value.code in {"citation_not_found", "invalid_structure"}
        accepting = _CiteClient("P1")
        output, assembly, usage, key, hit = _execute_batch(
            db, job, payload, batch, accepting, force=True
        )
        assert len(output.questions) == 2
        assert "P2" not in assembly.ref_map.targets
        assert hit is False

def test_single_path_rejects_dropped_ref_then_accepts_kept_ref(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        generation,
        "settings",
        replace(base_settings, model_max_request_mb=1),
    )
    monkeypatch.setattr(generation, "assemble_evidence", lambda db, job: _assembly())
    payload = {
        "idempotency_key": f"single-actual-{tmp_path.name}",
        "sources": [],
        "progress_text": "test",
        "difficulty": "consolidate",
        "question_count": 2,
        "plan_version_id": "pv_single_actual",
        "plan_constraints": {},
        "scope_text": "test",
    }
    with SessionLocal() as db:
        failing_payload = {**payload, "idempotency_key": f"{payload['idempotency_key']}-fail"}
        job, _ = create_job(db, failing_payload)
        db.commit()
        assert mark_job_running(job.id)
        db.refresh(job)
        failing = _CiteClient("P2")
        with pytest.raises(ValidationError) as excinfo:
            _run_single(db, job, failing_payload, failing)
        assert excinfo.value.code == "citation_not_found"

        accepting_payload = {**payload, "idempotency_key": f"{payload['idempotency_key']}-ok"}
        accepted_job, _ = create_job(db, accepting_payload)
        db.commit()
        assert mark_job_running(accepted_job.id)
        db.refresh(accepted_job)
        accepting = _CiteClient("P1")
        output, assembly, warnings, usage = _run_single(
            db, accepted_job, accepting_payload, accepting
        )
        assert len(output.questions) == 2
        assert "P2" not in assembly.ref_map.targets
