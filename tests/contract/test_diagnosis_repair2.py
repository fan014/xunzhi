from __future__ import annotations

import time

from sqlalchemy import select

from app.domain import generation
from app.domain.models import GenerationJob, now_iso
from app.infra.db import SessionLocal, recover_interrupted_jobs
from tests import stage4_helpers as helpers
from tests.stage4_helpers import wait_job


def _new_confirmed_session(client, tmp_path, color=(101, 111, 121)):
    source, _ = helpers.generate_pack(client, tmp_path, color=color, count=3)
    started = client.post(
        f"/api/sources/{source['id']}/diagnosis/sessions",
        json={"idempotency_key": f"repair2-{time.time_ns()}", "goal": "补薄弱点", "question_count": 3},
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
                "idempotency_key": f"repair2-a-{time.time_ns()}",
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
        json={"idempotency_key": f"repair2-c-{time.time_ns()}"},
    )


def test_start_failure_after_job_persisted_then_restart_same_job(client, tmp_path, monkeypatch):
    _, session_id = _new_confirmed_session(client, tmp_path)
    original = generation.start_job

    def boom(job_id):
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(generation, "start_job", boom)
    first = _confirm(client, session_id)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["job_status"] == "queued"
    assert payload["job_id"]
    assert payload["retry_available"] is True
    assert payload["error"]["code"] == "diagnostic_generation_start_failed"
    assert payload["pack_id"] is None

    monkeypatch.setattr(generation, "start_job", original)
    second = _confirm(client, session_id)
    assert second.status_code == 200, second.text
    assert second.json()["job_id"] == payload["job_id"]
    job = wait_job(client, payload["job_id"])
    assert job["status"] == "succeeded"
    assert job["attempt_no"] == 0
    assert job["retry_available"] is False


def test_claim_then_crash_before_executor_restart_same_job(client, tmp_path, monkeypatch):
    _, session_id = _new_confirmed_session(client, tmp_path)
    original = generation.start_job
    monkeypatch.setattr(generation, "start_job", lambda job_id: None)
    first = _confirm(client, session_id).json()
    job_id = first["job_id"]
    assert job_id
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        job.launch_state = "dispatching"
        db.commit()
    recover_interrupted_jobs()
    with SessionLocal() as db:
        job = db.get(GenerationJob, job_id)
        assert job.status == "queued"
        assert job.launch_state == "pending_dispatch"
        assert job.idempotency_key == f"diagnostic:{session_id}"
    monkeypatch.setattr(generation, "start_job", original)
    second = _confirm(client, session_id).json()
    assert second["job_id"] == job_id
    assert wait_job(client, job_id)["status"] == "succeeded"


def test_generation_attempts_cap_and_exhausted(client, tmp_path, monkeypatch):
    _, session_id = _new_confirmed_session(client, tmp_path)
    monkeypatch.setattr(generation, "start_job", lambda job_id: None)
    first = _confirm(client, session_id).json()
    base = None
    with SessionLocal() as db:
        base = db.get(GenerationJob, first["job_id"])
        for n in (1, 2, 3):
            db.add(
                GenerationJob(
                    id=f"job_retry_{session_id}_{n}",
                    idempotency_key=f"diagnostic:{session_id}:retry:{n}",
                    status="failed",
                    stage="fetch_sources",
                    stage_updated_at="9999-01-01T00:00:00Z",
                    request_json=base.request_json,
                    input_hash="deadbeef",
                    error_code="upstream_error",
                    error_detail="模拟失败",
                    created_at=f"9999-01-01T00:00:0{n}Z",
                    updated_at=f"9999-01-01T00:00:0{n}Z",
                    launch_state=None,
                )
            )
        db.commit()
    exhausted = _confirm(client, session_id)
    assert exhausted.status_code == 200, exhausted.text
    body = exhausted.json()
    assert body["error"]["code"] == "diagnostic_generation_retry_exhausted"
    assert body["error"]["retryable"] is False
    assert body["retry_available"] is False
    assert body["job_id"] == f"job_retry_{session_id}_3"
    assert body["job_status"] == "failed"
    with SessionLocal() as db:
        rows = db.execute(
            select(GenerationJob).where(
                GenerationJob.idempotency_key.like(f"diagnostic:{session_id}%")
            )
        ).scalars().all()
        assert len(rows) == 4


def test_create_failure_not_persisted_rebuilds_same_attempt(client, tmp_path, monkeypatch):
    _, session_id = _new_confirmed_session(client, tmp_path)
    original = generation.create_job

    def boom(db, payload):
        raise RuntimeError("create failed")

    monkeypatch.setattr(generation, "create_job", boom)
    failed = _confirm(client, session_id)
    assert failed.status_code == 200, failed.text
    assert failed.json()["job_id"] is None
    assert failed.json()["error"]["code"] == "generation_create_failed"
    assert failed.json()["retry_available"] is True
    monkeypatch.setattr(generation, "create_job", original)
    rebuilt = _confirm(client, session_id)
    assert rebuilt.status_code == 200, rebuilt.text
    with SessionLocal() as db:
        job = db.get(GenerationJob, rebuilt.json()["job_id"])
        assert job.idempotency_key == f"diagnostic:{session_id}"
        retry_one = db.execute(
            select(GenerationJob).where(
                GenerationJob.idempotency_key == f"diagnostic:{session_id}:retry:1"
            )
        ).scalars().first()
        assert retry_one is None


def test_job_endpoint_exposes_attempt_and_retry(client, tmp_path, monkeypatch):
    _, session_id = _new_confirmed_session(client, tmp_path)

    def boom(job_id):
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(generation, "start_job", boom)
    payload = _confirm(client, session_id).json()
    job = client.get(f"/api/jobs/{payload['job_id']}").json()
    assert job["job_id"] == payload["job_id"]
    assert job["status"] == "queued"
    assert job["attempt_no"] == 0
    assert job["retry_available"] is True
    assert job["error"]["retryable"] is True
    for legacy in ("stage", "stage_updated_at", "elapsed_ms", "pack_id", "is_test_double"):
        assert legacy in job
    assert "launch_state" not in job


def test_restart_keeps_persisted_not_started_and_not_interrupted(client, tmp_path):
    marker = f"restart-{time.time_ns()}"
    with SessionLocal() as db:
        rows = {
            "queued-null": GenerationJob(
                id=f"job_{marker}_a",
                idempotency_key=f"diagnostic:{marker}:a",
                status="queued",
                stage="fetch_sources",
                stage_updated_at=now_iso(),
                request_json="{}",
                input_hash="x",
                created_at=now_iso(),
                updated_at=now_iso(),
                launch_state=None,
            ),
            "queued-dispatching": GenerationJob(
                id=f"job_{marker}_b",
                idempotency_key=f"diagnostic:{marker}:b",
                status="queued",
                stage="fetch_sources",
                stage_updated_at=now_iso(),
                request_json="{}",
                input_hash="x",
                created_at=now_iso(),
                updated_at=now_iso(),
                launch_state="dispatching",
            ),
            "running-dispatched": GenerationJob(
                id=f"job_{marker}_c",
                idempotency_key=f"diagnostic:{marker}:c",
                status="running",
                stage="fetch_sources",
                stage_updated_at=now_iso(),
                request_json="{}",
                input_hash="x",
                created_at=now_iso(),
                updated_at=now_iso(),
                launch_state="dispatched",
            ),
        }
        db.add_all(rows.values())
        db.commit()
    recover_interrupted_jobs()
    with SessionLocal() as db:
        assert db.get(GenerationJob, f"job_{marker}_a").status == "queued"
        assert db.get(GenerationJob, f"job_{marker}_a").launch_state is None
        released = db.get(GenerationJob, f"job_{marker}_b")
        assert released.status == "queued"
        assert released.launch_state == "pending_dispatch"
        interrupted = db.get(GenerationJob, f"job_{marker}_c")
        assert interrupted.status == "failed"
        assert interrupted.error_code == "interrupted"