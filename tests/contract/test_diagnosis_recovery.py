from __future__ import annotations

import time

from sqlalchemy import select

from app.domain import generation
from app.domain.models import GenerationJob, LearningPlanVersion, now_iso
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers
from tests.stage4_helpers import wait_job


def _prepare(client, tmp_path, color=(131, 141, 151)):
    source, _ = helpers.generate_pack(client, tmp_path, color=color, count=3)
    started = client.post(
        f"/api/sources/{source['id']}/diagnosis/sessions",
        json={"idempotency_key": f"recover-{time.time_ns()}", "goal": "补薄弱点", "question_count": 3},
    ).json()
    session_id = started["session"]["id"]
    for _ in range(2):
        current = client.get(f"/api/diagnosis/sessions/{session_id}").json()
        question = current.get("current_question")
        if question is None:
            break
        answered = client.post(
            f"/api/diagnosis/sessions/{session_id}/answers",
            json={
                "idempotency_key": f"recover-a-{time.time_ns()}",
                "question_id": question["id"],
                "selected_index": 0,
                "status": "answered",
            },
        )
        assert answered.status_code == 200, answered.text
    finish = client.post(
        f"/api/diagnosis/sessions/{session_id}/finish",
        json={"goal": "补薄弱点", "question_count": 3},
    )
    assert finish.status_code == 200, finish.text
    return source, session_id


def _confirm(client, session_id):
    return client.post(
        f"/api/diagnosis/sessions/{session_id}/confirm",
        json={"idempotency_key": f"recover-c-{time.time_ns()}"},
    )


def test_non_confirmed_session_generation_is_null(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(132, 142, 152), count=3)
    started = client.post(
        f"/api/sources/{source['id']}/diagnosis/sessions",
        json={"idempotency_key": f"recover-null-{time.time_ns()}", "goal": "补薄弱点", "question_count": 3},
    ).json()
    assert started["session"]["generation"] is None


def test_confirmed_open_pack_then_pack_missing_then_plan_missing(client, tmp_path):
    _, session_id = _prepare(client, tmp_path)
    confirmed = _confirm(client, session_id)
    assert confirmed.status_code == 200, confirmed.text
    job_id = confirmed.json()["job_id"]
    job = wait_job(client, job_id)
    assert job["status"] == "succeeded"
    loaded = client.get(f"/api/diagnosis/sessions/{session_id}").json()
    generation_payload = loaded["generation"]
    assert generation_payload["actions"] == ["open_pack"]
    assert generation_payload["pack_id"] == job["pack_id"]
    assert "plan_id" not in generation_payload

    version_id = loaded["plan_version_id"]
    with SessionLocal() as db:
        job_row = db.get(GenerationJob, job_id)
        job_row.pack_id = None
        db.commit()
    missing_pack = client.get(f"/api/diagnosis/sessions/{session_id}").json()["generation"]
    assert missing_pack["actions"] == ["open_plan"]
    assert missing_pack["pack_id"] is None
    assert missing_pack["retry_available"] is False
    assert missing_pack["error"]["code"] == "pack_missing"
    assert missing_pack["error"]["retryable"] is False
    assert missing_pack["plan_version_id"] == version_id
    with SessionLocal() as db:
        version = db.get(LearningPlanVersion, version_id)
        assert missing_pack["plan_id"] == version.plan_id

    with SessionLocal() as db:
        version = db.get(LearningPlanVersion, version_id)
        version.status = "draft"
        db.commit()
    unavailable = client.get(f"/api/diagnosis/sessions/{session_id}").json()["generation"]
    assert unavailable["actions"] == []
    assert unavailable["error"]["code"] == "plan_missing"
    assert "plan_id" not in unavailable


def test_generation_view_is_read_only(client, tmp_path):
    _, session_id = _prepare(client, tmp_path)
    confirmed = _confirm(client, session_id)
    job_id = confirmed.json()["job_id"]
    wait_job(client, job_id)
    with SessionLocal() as db:
        before = db.execute(select(GenerationJob)).scalars().all()
        before_count = len(before)
        attempt_before = [row.status for row in before]
    for _ in range(3):
        payload = client.get(f"/api/diagnosis/sessions/{session_id}").json()
        assert payload["generation"] is not None
    with SessionLocal() as db:
        after = db.execute(select(GenerationJob)).scalars().all()
        assert len(after) == before_count
        assert [row.status for row in after] == attempt_before


def test_no_gradable_question_two_confirms_reuse_same_job(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(133, 143, 153), count=3)
    started = client.post(
        f"/api/sources/{source['id']}/diagnosis/sessions",
        json={"idempotency_key": f"nograd-{time.time_ns()}", "goal": "补薄弱点", "question_count": 3},
    ).json()
    session_id = started["session"]["id"]
    for _ in range(5):
        current = client.get(f"/api/diagnosis/sessions/{session_id}").json()
        question = current.get("current_question")
        if question is None:
            break
        client.post(
            f"/api/diagnosis/sessions/{session_id}/answers",
            json={
                "idempotency_key": f"nograd-a-{time.time_ns()}",
                "question_id": question["id"],
                "selected_index": None,
                "status": "skipped",
            },
        )
    finish = client.post(
        f"/api/diagnosis/sessions/{session_id}/finish",
        json={"goal": "补薄弱点", "question_count": 3},
    )
    assert finish.status_code == 200, finish.text
    first = _confirm(client, session_id)
    second = _confirm(client, session_id)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["container_pack_id"] is None
    assert second.json()["container_pack_id"] is None
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["job_id"]
    with SessionLocal() as db:
        job_count = len(db.execute(select(GenerationJob)).scalars().all())
        assert job_count >= 1