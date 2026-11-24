from __future__ import annotations

import json
import time
from types import SimpleNamespace

from app.domain import diagnosis
from app.infra.db import SessionLocal
from app.infra.model_client import ModelError
from tests import stage4_helpers as helpers


class SpyClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        item = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            content_text=json.dumps(item, ensure_ascii=False),
            usage={},
            finish_reason="stop",
        )


class RawClient:
    def __init__(self, content_text, finish_reason="stop"):
        self.content_text = content_text
        self.finish_reason = finish_reason
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return SimpleNamespace(
            content_text=self.content_text,
            usage={},
            finish_reason=self.finish_reason,
        )


def _scope_spec(stem="你学过这个考点吗？", citation_ref=None):
    spec = {
        "question_type": "scope",
        "stem": stem,
        "options": ["学过", "没学过", "不确定"],
    }
    if citation_ref:
        spec["citation_ref"] = citation_ref
    return spec


def _first_ref(source_id):
    with SessionLocal() as db:
        return diagnosis._diagnosis_refs(db, source_id)[0]["ref"]


def _start(client, source_id):
    return client.post(
        f"/api/sources/{source_id}/diagnosis/sessions",
        json={"idempotency_key": f"turn-{time.time_ns()}", "goal": "补薄弱点", "question_count": 3},
    )


def test_injected_client_is_used_and_task_text_has_sections(client, tmp_path, monkeypatch):
    source, _ = helpers.generate_pack(client, tmp_path, color=(81, 91, 101), count=3)
    spy = SpyClient(_scope_spec(citation_ref=_first_ref(source["id"])))
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    assert spy.calls == 1
    assert response.json()["session"]["current_question"]["kind"] == "scope"
    text = spy.requests[0].task_text
    for section in ("本次目标", "已确认范围提示", "历史问答", "可用引用", "只输出一个 JSON 对象"):
        assert section in text
    current = response.json()["session"]["current_question"]
    assert "answer_index" not in current
    assert "explanation" not in current


def test_start_failure_persists_session_and_retry_resumes(client, tmp_path, monkeypatch):
    source, _ = helpers.generate_pack(client, tmp_path, color=(82, 92, 102), count=3)
    ref = _first_ref(source["id"])
    spy = SpyClient(ModelError("generation_timeout", "模型服务响应超时"), _scope_spec(citation_ref=ref))
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["error"]["code"] == "diagnostic_generation_timeout"
    assert payload["error"]["retryable"] is True
    assert payload["session"]["last_turn_error"]["round"] == 0
    session_id = payload["session"]["id"]
    retried = client.post(f"/api/diagnosis/sessions/{session_id}/retry")
    assert retried.status_code == 200, retried.text
    body = retried.json()
    assert "session" not in body
    assert body["current_round"] == 1
    assert body["last_turn_error"] is None
    assert body["current_question"]["kind"] == "scope"


def test_followup_failure_keeps_answer_and_retry_resumes(client, tmp_path, monkeypatch):
    source, _ = helpers.generate_pack(client, tmp_path, color=(83, 93, 103), count=3)
    ref = _first_ref(source["id"])
    spy = SpyClient(
        _scope_spec(citation_ref=ref),
        ModelError("generation_timeout", "模型服务响应超时"),
        {
            "question_type": "focus",
            "stem": "先学哪一部分？",
            "options": ["交换机转发", "路由协议"],
            "citation_ref": ref,
        },
    )
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    started = _start(client, source["id"]).json()["session"]
    question = started["current_question"]
    answered = client.post(
        f"/api/diagnosis/sessions/{started['id']}/answers",
        json={
            "idempotency_key": f"turn-a-{time.time_ns()}",
            "question_id": question["id"],
            "selected_index": 0,
            "status": "answered",
        },
    )
    assert answered.status_code == 200, answered.text
    failed = answered.json()
    assert failed["error"]["code"] == "diagnostic_generation_timeout"
    assert failed["current_round"] == 1
    assert len(failed["confirmed_info"]) == 1
    assert failed["last_turn_error"]["round"] == 1
    retried = client.post(f"/api/diagnosis/sessions/{started['id']}/retry")
    assert retried.status_code == 200, retried.text
    resumed = retried.json()
    assert resumed["current_round"] == 2
    assert len(resumed["confirmed_info"]) == 1
    assert resumed["last_turn_error"] is None


def test_output_validation_returns_2xx_with_error(client, tmp_path, monkeypatch):
    source, _ = helpers.generate_pack(client, tmp_path, color=(84, 94, 104), count=3)
    spy = SpyClient({"question_type": "scope", "stem": "", "options": []})
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    assert response.json()["error"]["code"] == "diagnostic_invalid_structure"


def test_citation_out_of_scope_returns_2xx_with_error(client, tmp_path, monkeypatch):
    source, _ = helpers.generate_pack(client, tmp_path, color=(85, 95, 105), count=3)
    spy = SpyClient({**_scope_spec(), "citation_ref": "T99"})
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    assert response.json()["error"]["code"] == "diagnostic_citation_out_of_scope"


def test_request_param_errors_stay_4xx(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(86, 96, 106), count=3)
    started = _start(client, source["id"]).json()["session"]
    question = started["current_question"]
    invalid = client.post(
        f"/api/diagnosis/sessions/{started['id']}/answers",
        json={
            "idempotency_key": f"turn-bad-{time.time_ns()}",
            "question_id": question["id"],
            "selected_index": 99,
            "status": "answered",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "diagnostic_answer_invalid"
    missing = client.post(
        "/api/diagnosis/sessions/does-not-exist/answers",
        json={"idempotency_key": "missing", "question_id": "x", "selected_index": 0, "status": "answered"},
    )
    assert missing.status_code == 404


def test_no_key_returns_201_with_session(client, tmp_path, monkeypatch):
    from app.infra.model_client import DeepSeekClient

    source, _ = helpers.generate_pack(client, tmp_path, color=(87, 97, 107), count=3)
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: DeepSeekClient(api_key=""))
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["session"]["id"]
    assert payload["error"]["code"] == "diagnostic_no_api_key"

def test_fenced_json_output_is_parsed(client, tmp_path, monkeypatch):
    source, _ = helpers.generate_pack(client, tmp_path, color=(88, 98, 108), count=3)
    spec = _scope_spec(citation_ref=_first_ref(source["id"]))
    wrapped = f"```json\n{json.dumps(spec, ensure_ascii=False)}\n```"
    spy = RawClient(wrapped)
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    payload = response.json()
    assert "error" not in payload
    assert payload["session"]["current_question"]["kind"] == "scope"


def test_empty_model_output_is_invalid_structure_and_retryable(client, tmp_path, monkeypatch):
    source, _ = helpers.generate_pack(client, tmp_path, color=(89, 99, 109), count=3)
    spy = RawClient("   ")
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["error"]["code"] == "diagnostic_invalid_structure"
    assert payload["error"]["retryable"] is True
    assert payload["error"]["message"] == "模型返回内容为空"
    session_id = payload["session"]["id"]
    snapshot = payload["session"]
    assert snapshot["last_turn_error"]["code"] == "diagnostic_invalid_structure"
    retried = client.post(f"/api/diagnosis/sessions/{session_id}/retry")
    assert retried.status_code == 200, retried.text


def test_truncated_model_output_reports_truncation(client, tmp_path, monkeypatch):
    source, _ = helpers.generate_pack(client, tmp_path, color=(90, 100, 110), count=3)
    spy = RawClient('{"question_type": "scope", "stem": "你学过', finish_reason="length")
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["error"]["code"] == "diagnostic_invalid_structure"
    assert payload["error"]["message"] == "模型返回内容被截断"
    assert payload["error"]["retryable"] is True

def test_json_array_output_is_rejected_as_non_object(client, tmp_path, monkeypatch):
    source, _ = helpers.generate_pack(client, tmp_path, color=(91, 101, 111), count=3)
    spy = RawClient("[]")
    monkeypatch.setattr(diagnosis, "get_model_client", lambda: spy)
    response = _start(client, source["id"])
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["error"]["code"] == "diagnostic_invalid_structure"
    assert payload["error"]["message"] == "模型返回内容不是 JSON 对象"