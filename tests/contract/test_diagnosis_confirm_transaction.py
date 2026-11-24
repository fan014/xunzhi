from __future__ import annotations

import time

from sqlalchemy import select

from app.domain import diagnosis, generation
from app.domain.models import (
    Attempt,
    DiagnosticSession,
    GenerationJob,
    LearningEvidence,
    LearningPack,
    LearningPlan,
    LearningPlanVersion,
    now_iso,
)
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers
from tests.stage4_helpers import wait_job


def _prepare(client, tmp_path, color=(141, 151, 161)):
    source, _ = helpers.generate_pack(client, tmp_path, color=color, count=3)
    started = client.post(
        f"/api/sources/{source['id']}/diagnosis/sessions",
        json={"idempotency_key": f"tx-{time.time_ns()}", "goal": "补薄弱点", "question_count": 3},
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
                "idempotency_key": f"tx-a-{time.time_ns()}",
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
        json={"idempotency_key": f"tx-c-{time.time_ns()}"},
    )


def test_confirm_transaction_atomic_on_failure_before_commit(client, tmp_path, monkeypatch):
    _, session_id = _prepare(client, tmp_path)

    def boom(db, session):
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(diagnosis, "_after_confirm_writes", boom)
    failed = _confirm(client, session_id)
    assert failed.status_code == 500, failed.text
    assert failed.json()["error"]["code"] == "internal_error"
    with SessionLocal() as db:
        session = db.get(DiagnosticSession, session_id)
        assert session.status in {"active", "preview"}
        assert session.confirmed_at is None
        assert db.execute(select(LearningPack).where(LearningPack.job_id == f"diagnostic:{session_id}")).scalars().first() is None
        assert db.execute(select(Attempt).where(Attempt.idempotency_key == f"diagnostic:{session_id}")).scalars().first() is None
        assert db.execute(select(GenerationJob).where(GenerationJob.idempotency_key == f"diagnostic:{session_id}")).scalars().first() is None
        version = db.get(LearningPlanVersion, session.plan_version_id)
        assert version.status == "draft"

    monkeypatch.setattr(diagnosis, "_after_confirm_writes", lambda db, session: None)
    recovered = _confirm(client, session_id)
    assert recovered.status_code == 200, recovered.text
    with SessionLocal() as db:
        assert db.get(DiagnosticSession, session_id).status == "confirmed"
        assert db.execute(select(LearningPack).where(LearningPack.job_id == f"diagnostic:{session_id}")).scalars().first() is not None
        assert db.execute(select(Attempt).where(Attempt.idempotency_key == f"diagnostic:{session_id}")).scalars().first() is not None
        assert len(db.execute(select(GenerationJob).where(GenerationJob.idempotency_key.like(f"diagnostic:{session_id}%"))).scalars().all()) == 1


def test_gradable_question_with_missing_container_is_409_even_with_job(client, tmp_path, monkeypatch):
    _, session_id = _prepare(client, tmp_path)
    original_start = generation.start_job
    monkeypatch.setattr(generation, "start_job", lambda job_id: None)
    first = _confirm(client, session_id)
    assert first.status_code == 200
    job_id = first.json()["job_id"]
    assert job_id
    monkeypatch.setattr(generation, "start_job", original_start)
    with SessionLocal() as db:
        pack = db.execute(select(LearningPack).where(LearningPack.job_id == f"diagnostic:{session_id}")).scalars().first()
        if pack is not None:
            db.delete(pack)
            db.commit()
        assert db.execute(select(LearningPack).where(LearningPack.job_id == f"diagnostic:{session_id}")).scalars().first() is None
    monkeypatch.setattr(diagnosis, "_write_diagnostic_evidence_once", lambda *args, **kwargs: None)
    conflict = _confirm(client, session_id)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "diagnostic_confirm_conflict"
    with SessionLocal() as db:
        assert db.get(GenerationJob, job_id) is not None


def test_legacy_repair_keeps_newer_plan_pointer_and_confirmed_at(client, tmp_path):
    _, session_id = _prepare(client, tmp_path)
    first = _confirm(client, session_id)
    job_id = first.json()["job_id"]
    wait_job(client, job_id)
    with SessionLocal() as db:
        session = db.get(DiagnosticSession, session_id)
        original_confirmed_at = session.confirmed_at
        version = db.get(LearningPlanVersion, session.plan_version_id)
        plan = db.get(LearningPlan, version.plan_id)
        newer = LearningPlanVersion(
            id=f"planver_new_{time.time_ns()}",
            plan_id=plan.id,
            status="confirmed",
            origin=version.origin,
            adjustment_json=version.adjustment_json,
            sources_json=version.sources_json,
            scope_json=version.scope_json,
            items_json=version.items_json,
            question_count=version.question_count,
            handout_advice=version.handout_advice,
            created_at="9999-01-01T00:00:00Z",

            confirmed_at="9999-01-01T00:00:00Z",
        )
        db.add(newer)
        version.status = "draft"
        plan.current_version_id = newer.id
        db.commit()
        old_version_id = version.id
    with SessionLocal() as db:
        pack = db.execute(select(LearningPack).where(LearningPack.job_id == f"diagnostic:{session_id}")).scalars().first()
        db.delete(pack)
        db.commit()
    repaired = _confirm(client, session_id)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["duplicate"] is True
    with SessionLocal() as db:
        session = db.get(DiagnosticSession, session_id)
        version = db.get(LearningPlanVersion, old_version_id)
        plan = db.get(LearningPlan, version.plan_id)
        assert session.confirmed_at == original_confirmed_at
        assert version.status == "confirmed"
        assert plan.current_version_id != old_version_id
        packs = db.execute(select(LearningPack).where(LearningPack.job_id == f"diagnostic:{session_id}")).scalars().all()
        assert len(packs) == 1
        evidence = db.execute(select(LearningEvidence).where(LearningEvidence.source_type == "diagnostic")).scalars().all()
        assert evidence
        assert len(db.execute(select(GenerationJob).where(GenerationJob.idempotency_key.like(f"diagnostic:{session_id}%"))).scalars().all()) == 1

def test_busy_retry_confirms_session_and_version_before_task(client, tmp_path, monkeypatch):
    from sqlalchemy.exc import OperationalError

    source, _ = helpers.generate_pack(client, tmp_path, color=(142, 152, 162), count=3)
    started = client.post(
        f"/api/sources/{source['id']}/diagnosis/sessions",
        json={"idempotency_key": f"busy-{time.time_ns()}", "goal": "补薄弱点", "question_count": 3},
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
                "idempotency_key": f"busy-a-{time.time_ns()}",
                "question_id": question["id"],
                "selected_index": None,
                "status": "skipped",
            },
        )
    assert client.post(
        f"/api/diagnosis/sessions/{session_id}/finish",
        json={"goal": "补薄弱点", "question_count": 3},
    ).status_code == 200

    def busy(db, session, source, schedule_options=None):
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(diagnosis, "_confirm_write_transaction", busy)
    response = _confirm(client, session_id)
    assert response.status_code == 200, response.text
    assert response.json()["container_pack_id"] is None
    with SessionLocal() as db:
        session = db.get(DiagnosticSession, session_id)
        version = db.get(LearningPlanVersion, session.plan_version_id)
        plan = db.get(LearningPlan, version.plan_id)
        assert session.status == "confirmed"
        assert session.confirmed_at is not None
        assert version.status == "confirmed"
        assert plan.current_version_id == version.id
        assert db.execute(
            select(LearningPack).where(LearningPack.job_id == f"diagnostic:{session_id}")
        ).scalars().first() is None
        assert db.execute(
            select(LearningEvidence).where(
                LearningEvidence.source_type == "diagnostic",
                LearningEvidence.source_id == source["id"],
            )
        ).scalars().first() is None
        assert len(
            db.execute(
                select(GenerationJob).where(
                    GenerationJob.idempotency_key.like(f"diagnostic:{session_id}%")
                )
            ).scalars().all()
        ) == 1
