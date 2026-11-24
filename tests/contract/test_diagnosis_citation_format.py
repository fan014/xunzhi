from __future__ import annotations

import json
import time
from types import SimpleNamespace

from sqlalchemy import func, select

from app.domain import diagnosis
from app.domain.models import DiagnosticQuestion, DiagnosticSession, LearningEvidence
from app.infra.db import SessionLocal
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


def _upload_text(client, tmp_path, name: str) -> dict:
    path = tmp_path / name
    path.write_text(
        "交换机根据 MAC 地址表转发帧。路由器根据路由表转发跨网段分组。",
        encoding="utf-8",
    )
    return helpers.upload(client, path)


def _refs(source_id: str) -> list[dict]:
    with SessionLocal() as db:
        return diagnosis._diagnosis_refs(db, source_id)


def _start(client, source_id: str):
    return client.post(
        f"/api/sources/{source_id}/diagnosis/sessions",
        json={
            "idempotency_key": f"ref-format-{time.time_ns()}",
            "goal": "补薄弱点",
            "question_count": 3,
        },
    )


def _scope(reference: str, stem: str = "你学过交换机转发吗？") -> dict:
    return {
        "question_type": "scope",
        "stem": stem,
        "options": ["学过", "没学过"],
        "citation_ref": reference,
    }


def _count(rows) -> int:
    return int(rows)


def _counts(session_id: str) -> tuple[int, int]:
    with SessionLocal() as db:
        questions = db.execute(
            select(func.count())
            .select_from(DiagnosticQuestion)
            .where(DiagnosticQuestion.session_id == session_id)
        ).scalar_one()
        evidence = db.execute(select(func.count()).select_from(LearningEvidence)).scalar_one()
    return _count(questions), _count(evidence)


def test_bracketed_citation_is_normalized_to_whitelist_ref(client, tmp_path, monkeypatch):
    source = _upload_text(client, tmp_path, "ref_bracket.txt")
    ref = _refs(source["id"])[0]["ref"]
    spy = SpyClient(_scope(f"[{ref}]"))
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload.get("error") is None
    question = payload["session"]["current_question"]
    assert question is not None
    assert question["citations"][0]["ref"] == ref
    assert payload["session"]["last_turn_error"] is None


def test_decorated_citation_with_trailing_punctuation_is_accepted(client, tmp_path, monkeypatch):
    source = _upload_text(client, tmp_path, "ref_punct.txt")
    ref = _refs(source["id"])[0]["ref"]
    spy = SpyClient(_scope(f"【{ref}】。"))
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    assert response.json()["session"]["current_question"]["citations"][0]["ref"] == ref


def test_unknown_or_trimmed_citation_still_rejected_without_evidence(client, tmp_path, monkeypatch):
    source = _upload_text(client, tmp_path, "ref_unknown.txt")
    before = _counts("")
    spy = SpyClient(_scope("[T99]"))
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["error"]["code"] == "diagnostic_citation_out_of_scope"
    assert "引用不在本次资料范围内" in payload["error"]["message"]
    session = payload["session"]
    assert session["current_question"] is None
    assert session["last_turn_error"]["code"] == "diagnostic_citation_out_of_scope"
    after = _counts(session["id"])
    assert after[0] == 0
    assert after[1] == before[1]


def test_non_ref_text_still_rejected(client, tmp_path, monkeypatch):
    source = _upload_text(client, tmp_path, "ref_text.txt")
    spy = SpyClient(_scope("见教材第 3 页"))
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["error"]["code"] == "diagnostic_citation_out_of_scope"
    assert "格式不合法" in payload["error"]["message"]


def test_retry_after_first_turn_failure_restores_question(client, tmp_path, monkeypatch):
    source = _upload_text(client, tmp_path, "ref_retry.txt")
    ref = _refs(source["id"])[0]["ref"]
    with SessionLocal() as db:
        evidence_before = int(
            db.execute(select(func.count()).select_from(LearningEvidence)).scalar_one()
        )
    spy = SpyClient(_scope("[T99]"), _scope(f"[{ref}]", "你学过跨网段转发吗？"))
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    first = _start(client, source["id"])
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["error"]["code"] == "diagnostic_citation_out_of_scope"
    session_id = payload["session"]["id"]
    assert payload["session"]["last_turn_error"]["code"] == "diagnostic_citation_out_of_scope"

    retried = client.post(f"/api/diagnosis/sessions/{session_id}/retry")
    assert retried.status_code == 200, retried.text
    body = retried.json()
    assert body.get("error") is None
    question = body["current_question"]
    assert question is not None
    assert question["stem"] == "你学过跨网段转发吗？"
    assert question["citations"][0]["ref"] == ref
    assert body["last_turn_error"] is None
    with SessionLocal() as db:
        session = db.get(DiagnosticSession, session_id)
        assert session.status == "active"
        assert session.source_id == source["id"]
        assert session.target_goal == "补薄弱点"
        evidence = db.execute(select(func.count()).select_from(LearningEvidence)).scalar_one()
        assert int(evidence) == evidence_before