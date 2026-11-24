from __future__ import annotations

import json
import time
from types import SimpleNamespace

from sqlalchemy import select

from app.domain import diagnosis
from app.domain.models import Citation, DiagnosticSession, Evidence
from app.domain.validation import ValidationError
from app.infra.db import SessionLocal
from app.infra.model_client import ModelError
from tests import stage4_helpers as helpers


class SpyClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, request):
        item = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            content_text=json.dumps(item, ensure_ascii=False), usage={}, finish_reason="stop"
        )


def _upload_text(client, tmp_path, text="交换机根据 MAC 地址表转发帧。路由器根据路由表转发分组。"):
    path = tmp_path / f"notes_{time.time_ns()}.txt"
    path.write_text(text, encoding="utf-8")
    return helpers.upload(client, path)


def _start(client, source_id):
    return client.post(
        f"/api/sources/{source_id}/diagnosis/sessions",
        json={"idempotency_key": f"wl-{time.time_ns()}", "goal": "补薄弱点", "question_count": 3},
    )


def _refs(source_id):
    with SessionLocal() as db:
        return diagnosis._diagnosis_refs(db, source_id)


def _state(session_id):
    with SessionLocal() as db:
        session = db.get(DiagnosticSession, session_id)
        return diagnosis._state(session)


def test_first_turn_uses_source_evidence_without_citation(client, tmp_path, monkeypatch):
    source = _upload_text(client, tmp_path)
    with SessionLocal() as db:
        assert db.execute(
            select(Citation).where(Citation.source_id == source["id"])
        ).scalars().first() is None
    refs = _refs(source["id"])
    assert refs and refs[0]["kind"] == "text_span"
    ref = refs[0]["ref"]
    spy = SpyClient(
        {
            "question_type": "scope",
            "stem": "你学过交换机转发吗？",
            "options": ["学过", "没学过"],
            "citation_ref": ref,
        }
    )
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload.get("error") is None
    question = payload["session"]["current_question"]
    assert question["kind"] == "scope"
    assert question["citations"][0]["ref"] == ref
    assert question["citations"][0]["evidence_id"] == refs[0]["evidence_id"]
    state = _state(payload["session"]["id"])
    assert ref in state["ref_whitelist"]


def test_ref_whitelist_survives_first_timeout_and_retry(client, tmp_path, monkeypatch):
    source = _upload_text(client, tmp_path)
    ref = _refs(source["id"])[0]["ref"]
    spy = SpyClient(
        ModelError("generation_timeout", "模型超时"),
        {
            "question_type": "scope",
            "stem": "你学过交换机转发吗？",
            "options": ["学过", "没学过"],
            "citation_ref": ref,
        },
    )
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    first = _start(client, source["id"])
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["error"]["code"] == "diagnostic_generation_timeout"
    state = _state(payload["session"]["id"])
    assert ref in state["ref_whitelist"]
    evidence_id = state["ref_whitelist"][ref]["evidence_id"]
    retried = client.post(f"/api/diagnosis/sessions/{payload['session']['id']}/retry")
    assert retried.status_code == 200, retried.text
    question = retried.json()["current_question"]
    assert question["citations"][0]["ref"] == ref
    assert question["citations"][0]["evidence_id"] == evidence_id


def test_empty_first_build_can_rebuild_after_evidence(client, tmp_path, monkeypatch):
    source = _upload_text(client, tmp_path)
    calls = {"n": 0}

    def no_evidence(db, session, **kwargs):
        calls["n"] += 1
        raise ValidationError("insufficient_evidence", "本次范围内没有可用证据")

    original = diagnosis.assemble_for_sources
    monkeypatch.setattr(diagnosis, "assemble_for_sources", no_evidence)
    ref = "T1"
    spy = SpyClient(
        {
            "question_type": "scope",
            "stem": "你学过交换机转发吗？",
            "options": ["学过", "没学过"],
            "citation_ref": ref,
        }
    )
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    first = _start(client, source["id"])
    assert first.status_code == 201, first.text
    assert first.json()["error"]["code"] == "diagnostic_insufficient_evidence"
    state = _state(first.json()["session"]["id"])
    assert "ref_whitelist" not in state
    monkeypatch.setattr(diagnosis, "assemble_for_sources", original)
    retried = client.post(f"/api/diagnosis/sessions/{first.json()['session']['id']}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["current_question"]["citations"]


def test_image_label_only_rejects_gradable_question(client, tmp_path, monkeypatch):
    source = helpers.upload(client, helpers.make_png(tmp_path / f"img_{time.time_ns()}.png"))
    refs = [item for item in _refs(source["id"]) if item["kind"] != "text_span"]
    assert refs
    ref = refs[0]["ref"]
    spy = SpyClient(
        {
            "question_type": "probe_question",
            "stem": "图中两台设备是否在同一交换机下？",
            "options": ["是", "不是"],
            "answer_index": 0,
            "explanation": "看图可以判断。",
            "citation_ref": ref,
        }
    )
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    assert response.json()["error"]["code"] == "diagnostic_invalid_structure"
    assert response.json()["session"]["current_question"] is None
    spy.responses.append(
        {
            "question_type": "scope",
            "stem": "你学过交换机转发吗？",
            "options": ["学过", "没学过"],
            "citation_ref": ref,
        }
    )
    retried = client.post(f"/api/diagnosis/sessions/{response.json()['session']['id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["current_question"]["kind"] == "scope"