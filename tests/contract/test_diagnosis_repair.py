from __future__ import annotations

import json
import time
from types import SimpleNamespace


from app.infra.db import SessionLocal
from app.domain import diagnosis
from app.domain.models import LearningEvidence, LearningPack
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers
from sqlalchemy import select


def _start(client, source_id, key, goal="补上薄弱点", count=3):
    response = client.post(
        f"/api/sources/{source_id}/diagnosis/sessions",
        json={"idempotency_key": key, "goal": goal, "question_count": count},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _answer(client, session, index, status="answered"):
    question = session["session"]["current_question"]
    response = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/answers",
        json={
            "idempotency_key": f"a-{time.time_ns()}",
            "question_id": question["id"],
            "selected_index": index,
            "status": status,
        },
    )
    assert response.status_code == 200, response.text
    return {"session": response.json()}


def test_page_route_and_formal_pack(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(71, 81, 91), count=3)
    session = _start(client, source["id"], f"page-{time.time_ns()}")
    session_id = session["session"]["id"]
    page = client.get(f"/diagnosis/{session_id}")
    assert page.status_code == 200, page.text
    assert client.get(f"/api/diagnosis/{session_id}").status_code == 404

    session = _answer(client, session, 0)
    session = _answer(client, session, 0)
    session = _answer(client, session, 0)
    finish = client.post(
        f"/api/diagnosis/sessions/{session_id}/finish",
        json={"goal": "补上薄弱点", "question_count": 3},
    )
    assert finish.status_code == 200, finish.text
    confirmed = client.post(
        f"/api/diagnosis/sessions/{session_id}/confirm",
        json={"idempotency_key": f"c-{time.time_ns()}"},
    )
    assert confirmed.status_code == 200, confirmed.text
    payload = confirmed.json()
    assert payload["job_id"]
    assert payload["pack_id"] is None or isinstance(payload["pack_id"], str)
    job = helpers.wait_job(client, payload["job_id"])
    assert job["status"] == "succeeded", job
    formal = client.get(f"/api/packs/{job['pack_id']}")
    assert formal.status_code == 200, formal.text
    if payload["container_pack_id"]:
        assert client.get(f"/api/packs/{payload['container_pack_id']}").status_code == 404


def test_question_count_five_persists(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(72, 82, 92), count=3)
    session = _start(client, source["id"], f"count-{time.time_ns()}", count=5)
    assert session["session"]["question_count"] == 5
    finish = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/finish",
        json={"goal": "五题复习", "question_count": 5},
    )
    assert finish.status_code == 200, finish.text
    assert finish.json()["version"]["question_count"] == 5


def test_real_model_path_calls_client(client, tmp_path, monkeypatch):
    source, _ = helpers.generate_pack(client, tmp_path, color=(73, 83, 93), count=3)

    class Client:
        def __init__(self, citation_ref):
            self.calls = 0
            self.citation_ref = citation_ref

        def generate(self, request):
            self.calls += 1
            return SimpleNamespace(
                content_text=json.dumps(
                    {
                        "question_type": "scope",
                        "stem": "你学过这个考点吗？",
                        "options": ["学过", "没学过"],
                        "citation_ref": self.citation_ref,
                    },
                    ensure_ascii=False,
                )
            )

    with SessionLocal() as db:
        citation_ref = diagnosis._diagnosis_refs(db, source["id"])[0]["ref"]
    fake = Client(citation_ref)

    monkeypatch.setattr(diagnosis, "get_model_client", lambda: fake)
    response = client.post(
        f"/api/sources/{source['id']}/diagnosis/sessions",
        json={"idempotency_key": f"real-{time.time_ns()}", "goal": "补薄弱点", "question_count": 3},
    )
    assert response.status_code == 201, response.text
    assert fake.calls == 1
    assert response.json()["session"]["current_question"]["kind"] == "scope"


def test_skip_allows_early_finish_without_evidence(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(74, 84, 94), count=3)
    with SessionLocal() as db:
        before = len(db.execute(select(LearningEvidence)).scalars().all())
    session = _start(client, source["id"], f"skip-{time.time_ns()}")
    assert session["session"]["can_finish"] is True
    session = _answer(client, session, None, status="skipped")
    finish = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/finish",
        json={"goal": "补薄弱点", "question_count": 3},
    )
    assert finish.status_code == 200, finish.text
    client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/confirm",
        json={"idempotency_key": f"c-{time.time_ns()}"},
    )
    with SessionLocal() as db:
        assert len(db.execute(select(LearningEvidence)).scalars().all()) == before