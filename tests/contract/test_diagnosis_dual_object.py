from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select

from app.domain import diagnosis
from app.domain.models import DiagnosticQuestion
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "model_output"
    / "diagnosis_dual_object.txt"
)


class SequenceClient:
    def __init__(self, *items):
        self.items = list(items)
        self.calls = 0
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        item = self.items[min(self.calls, len(self.items) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            content, finish_reason = item
        elif isinstance(item, str):
            content, finish_reason = item, "stop"
        else:
            content, finish_reason = json.dumps(item, ensure_ascii=False), "stop"
        return SimpleNamespace(
            content_text=content,
            finish_reason=finish_reason,
            usage={},
            request_stats={},
        )


def _upload(client, tmp_path) -> dict:
    note = tmp_path / f"diagnosis_dual_{time.time_ns()}.txt"
    note.write_text(
        "IPv4 子网划分：/26 的掩码是 255.255.255.192，"
        "192.168.1.0/26 与 192.168.1.64/26 属于不同子网，"
        "PC-A 192.168.1.10 和 PC-B 192.168.1.70 不在同一子网。\n\n"
        "TCP 快速重传：接收端连续收到三个重复 ACK 时，"
        "发送端立即重传丢失的报文段，不等待重传计时器到期。",
        encoding="utf-8",
    )
    return helpers.upload(client, note)


def _start(client, source_id: str, goal: str):
    return client.post(
        f"/api/sources/{source_id}/diagnosis/sessions",
        json={
            "idempotency_key": f"dual-object-{time.time_ns()}",
            "goal": goal,
            "question_count": 3,
        },
    )


def _question_count(session_id: str) -> int:
    with SessionLocal() as db:
        return int(
            db.execute(
                select(func.count())
                .select_from(DiagnosticQuestion)
                .where(DiagnosticQuestion.session_id == session_id)
            ).scalar_one()
        )


def _answer(client, session_id: str, question: dict, selected: int):
    return client.post(
        f"/api/diagnosis/sessions/{session_id}/answers",
        json={
            "idempotency_key": f"dual-answer-{time.time_ns()}",
            "question_id": question["id"],
            "selected_index": selected,
            "status": "answered",
        },
    )


def _scope(stem: str = "你学过这个考点吗？", reference: str = "T1") -> dict:
    return {
        "question_type": "scope",
        "stem": stem,
        "options": ["学过", "没学过"],
        "citation_ref": reference,
    }


def test_dual_object_response_shows_first_and_saves_one(client, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="xunzhi.diagnosis")
    source = _upload(client, tmp_path)
    dual = FIXTURE.read_text(encoding="utf-8")
    spy = SequenceClient(dual, _scope("你学过 TCP 快速重传吗？"))
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)

    response = _start(client, source["id"], "IPv4 子网与 TCP 快速重传")
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload.get("error") is None
    session = payload["session"]
    question = session["current_question"]
    assert question is not None
    assert "192.168.1.10" in question["stem"]
    assert question["citations"][0]["ref"] == "T1"
    assert _question_count(session["id"]) == 1
    assert spy.calls == 1

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "diagnosis parse ok" in message
        and "objects=2" in message
        and "used=first" in message
        and "recovered=True" in message
        for message in messages
    )
    assert "192.168.1.10" not in caplog.text

    answered = _answer(client, session["id"], question, 1)
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body.get("error") is None
    assert body["current_question"] is not None
    assert "TCP" in body["current_question"]["stem"]
    assert _question_count(session["id"]) == 2
    assert spy.calls == 2


def test_first_object_out_of_scope_citation_rejects_without_retry(client, tmp_path, monkeypatch):
    source = _upload(client, tmp_path)
    dual = FIXTURE.read_text(encoding="utf-8").replace(
        '"citation_ref":"T1"', '"citation_ref":"T99"', 1
    )
    spy = SequenceClient(dual)
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)

    response = _start(client, source["id"], "IPv4 子网与 TCP 快速重传")
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["error"]["code"] == "diagnostic_citation_out_of_scope"
    session = payload["session"]
    assert session["current_question"] is None
    assert session["last_turn_error"]["code"] == "diagnostic_citation_out_of_scope"
    assert _question_count(session["id"]) == 0
    assert spy.calls == 1


def test_truncated_second_object_fails_after_one_retry(client, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="xunzhi.diagnosis")
    source = _upload(client, tmp_path)
    first = FIXTURE.read_text(encoding="utf-8").splitlines()[0]
    broken = first + '\n{"question_type": "probe_question", "stem": "第二道残缺'
    spy = SequenceClient(broken, broken)
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)

    response = _start(client, source["id"], "IPv4 子网与 TCP 快速重传")
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["error"]["code"] == "diagnostic_invalid_structure"
    session = payload["session"]
    assert session["current_question"] is None
    assert session["last_turn_error"]["code"] == "diagnostic_invalid_structure"
    assert _question_count(session["id"]) == 0
    assert spy.calls == 2
    failures = [record.getMessage() for record in caplog.records if "diagnosis parse failed" in record.getMessage()]
    assert len(failures) == 2
    assert all("objects=1" in message and "position=" in message for message in failures)


def test_finish_reason_length_is_rejected_after_one_retry(client, tmp_path, monkeypatch):
    source = _upload(client, tmp_path)
    dual = FIXTURE.read_text(encoding="utf-8")
    spy = SequenceClient((dual, "length"), (dual, "length"))
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)

    response = _start(client, source["id"], "IPv4 子网与 TCP 快速重传")
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["error"]["code"] == "diagnostic_invalid_structure"
    assert payload["error"]["message"] == "模型返回内容被截断"
    assert _question_count(payload["session"]["id"]) == 0
    assert spy.calls == 2


def test_unparseable_first_response_retries_once_with_correction(client, tmp_path, monkeypatch):
    source = _upload(client, tmp_path)
    spy = SequenceClient('{"question_type": "scope", "stem": "残缺', _scope("重试成功了吗？"))
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)

    response = _start(client, source["id"], "补薄弱点")
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload.get("error") is None
    assert payload["session"]["current_question"]["stem"] == "重试成功了吗？"
    assert _question_count(payload["session"]["id"]) == 1
    assert spy.calls == 2
    assert "格式纠正" in spy.requests[1].task_text
    assert "一个 JSON 对象" in spy.requests[1].task_text


def test_two_unparseable_responses_keep_retry_entry(client, tmp_path, monkeypatch):
    source = _upload(client, tmp_path)
    spy = SequenceClient('{"question_type": "scope", "stem": "第一次残缺', '[]')
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)

    response = _start(client, source["id"], "补薄弱点")
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["error"]["code"] == "diagnostic_invalid_structure"
    assert payload["error"]["retryable"] is True
    session = payload["session"]
    assert session["current_question"] is None
    assert session["status"] == "active"
    assert session["last_turn_error"]["message"]
    assert _question_count(session["id"]) == 0
    assert spy.calls == 2


def test_fenced_single_object_and_braces_in_stem(client, tmp_path, monkeypatch):
    source = _upload(client, tmp_path)
    payload = _scope('判断 {PC-A} 是否在 "192.168.1.0/24" 内？')
    fenced = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    spy = SequenceClient(fenced)
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)

    response = _start(client, source["id"], "IPv4 子网")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body.get("error") is None
    question = body["session"]["current_question"]
    assert question["stem"] == '判断 {PC-A} 是否在 "192.168.1.0/24" 内？'
    assert _question_count(body["session"]["id"]) == 1
    assert spy.calls == 1