from __future__ import annotations

import time

from sqlalchemy import select

from app.domain.models import (
    DiagnosticAnswer,
    DiagnosticQuestion,
    DiagnosticSession,
    LearningEvidence,
    LearningPack,
    QuestionKnowledgePoint,
)
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers
from tests.stage4_helpers import wait_job


def _points(pack: dict) -> list[dict]:
    with SessionLocal() as db:
        links = db.execute(
            select(QuestionKnowledgePoint).where(
                QuestionKnowledgePoint.question_id.in_([item["id"] for item in pack["questions"]])
            )
        ).scalars().all()
        return [
            {
                "id": link.knowledge_point_id,
                "name": db.get(__import__("app.domain.models", fromlist=["KnowledgePoint"]).KnowledgePoint, link.knowledge_point_id).display_name,
            }
            for link in links
        ]


def _start(client, source_id, key, goal="补上薄弱点"):
    response = client.post(
        f"/api/sources/{source_id}/diagnosis/sessions",
        json={"idempotency_key": key, "goal": goal, "question_count": 3},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _answer(client, session, index, key, status="answered"):
    question = session["session"]["current_question"]
    response = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/answers",
        json={
            "idempotency_key": key,
            "question_id": question["id"],
            "selected_index": index,
            "status": status,
        },
    )
    assert response.status_code == 200, response.text
    return {"session": response.json()}


def test_branch_and_flow(client, tmp_path):
    source, pack = helpers.generate_pack(client, tmp_path, color=(41, 51, 61), count=3)
    points = _points(pack)
    assert len(points) == 3

    advanced = _start(client, source["id"], f"branch-a-{time.time_ns()}")
    assert advanced["session"]["current_question"]["kind"] == "scope"
    advanced = _answer(client, advanced, 0, f"a1-{time.time_ns()}")
    assert advanced["session"]["current_question"]["kind"] == "diagnostic"

    beginner = _start(client, source["id"], f"branch-b-{time.time_ns()}")
    beginner = _answer(client, beginner, 1, f"b1-{time.time_ns()}")
    assert beginner["session"]["current_question"]["kind"] == "scope"
    assert beginner["session"]["current_question"]["knowledge_point_id"] != points[0]["id"]


def test_skip_writes_no_evidence(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(42, 52, 62), count=3)
    with SessionLocal() as db:
        before = db.execute(select(LearningEvidence)).scalars().all()
    session = _start(client, source["id"], f"skip-{time.time_ns()}")
    session = _answer(client, session, None, f"skip-answer-{time.time_ns()}", status="skipped")
    assert session["session"]["pending_items"]
    preview = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/finish",
        json={"goal": "补上薄弱点", "question_count": 3},
    )
    assert preview.status_code == 200, preview.text
    confirmed = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/confirm",
        json={"idempotency_key": f"confirm-{time.time_ns()}"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["pack_id"] is None
    assert confirmed.json()["container_pack_id"] is None
    with SessionLocal() as db:
        after = db.execute(select(LearningEvidence)).scalars().all()
    assert len(after) == len(before)


def test_reopen_invalidates_dependents(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(43, 53, 63), count=3)
    session = _start(client, source["id"], f"reopen-{time.time_ns()}")
    session = _answer(client, session, 0, f"reopen-a1-{time.time_ns()}")
    first_answer = session["session"]["confirmed_info"][0]
    reopened = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/answers/{first_answer['id']}/reopen"
    )
    assert reopened.status_code == 200, reopened.text
    transcript = client.get(
        f"/api/diagnosis/sessions/{session['session']['id']}/transcript"
    ).json()
    kinds = [q["kind"] for item in transcript["rounds"] for q in item["questions"]]
    assert "diagnostic" not in kinds
    assert reopened.json()["current_question"]["kind"] == "scope"


def test_diagnosis_writes_once_and_is_hidden(client, tmp_path):
    source, pack = helpers.generate_pack(client, tmp_path, color=(44, 54, 64), count=3)
    session = _start(client, source["id"], f"write-{time.time_ns()}")
    session = _answer(client, session, 0, f"write-a1-{time.time_ns()}")
    session = _answer(client, session, 0, f"write-a2-{time.time_ns()}")
    preview = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/finish",
        json={"goal": "补上薄弱点", "question_count": 3},
    )
    assert preview.status_code == 200, preview.text
    confirmed = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/confirm",
        json={"idempotency_key": f"confirm-{time.time_ns()}"},
    )
    assert confirmed.status_code == 200, confirmed.text
    container_id = confirmed.json()["container_pack_id"]
    job_id = confirmed.json()["job_id"]
    assert container_id and job_id
    job = wait_job(client, job_id)
    assert job["status"] == "succeeded", job
    pack_id = job["pack_id"]
    assert pack_id
    with SessionLocal() as db:
        diagnostic = db.execute(
            select(LearningEvidence).where(LearningEvidence.source_type == "diagnostic")
        ).scalars().all()
        assert diagnostic
        container = db.get(LearningPack, container_id)
        assert container.source_kind == "diagnostic"
    formal = client.get(f"/api/packs/{pack_id}")
    assert formal.status_code == 200, formal.text
    assert formal.json()["id"] == pack_id
    second = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/confirm",
        json={"idempotency_key": f"confirm-again-{time.time_ns()}"},
    )
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["job_id"] == job_id
    assert second.json()["pack_id"] == pack_id
    with SessionLocal() as db:
        count = len(
            db.execute(select(LearningEvidence).where(LearningEvidence.source_type == "diagnostic"))
            .scalars()
            .all()
        )
    assert count == len(diagnostic)

    listed = client.get("/api/packs?limit=200").json()["items"]
    assert all(item["id"] != container_id for item in listed)

    assert client.get(f"/api/packs/{container_id}").status_code == 404
    assert client.get(f"/api/packs/{container_id}/evidence").status_code == 404


def test_diagnostic_does_not_change_mastery(client, tmp_path):
    source, pack = helpers.generate_pack(client, tmp_path, color=(45, 55, 65), count=3)
    points = _points(pack)
    point_id = points[0]["id"]
    with SessionLocal() as db:
        q = db.execute(
            select(QuestionKnowledgePoint.question_id).where(
                QuestionKnowledgePoint.knowledge_point_id == point_id
            )
        ).scalars().first()
        question = db.get(__import__("app.domain.models", fromlist=["Question"]).Question, q)
        correct = question.answer_index
    client.post(
        f"/api/packs/{pack['id']}/submit",
        json={
            "idempotency_key": f"mastery-{time.time_ns()}",
            "answers": [{"question_id": q, "selected_index": correct}],
        },
    )
    before = client.get(f"/api/knowledge-points/{point_id}/mastery").json()["mastery"]

    session = _start(client, source["id"], f"mastery-dx-{time.time_ns()}")
    session = _answer(client, session, 0, f"mastery-a1-{time.time_ns()}")
    session = _answer(client, session, 1, f"mastery-a2-{time.time_ns()}")
    preview = client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/finish",
        json={"goal": "补上薄弱点", "question_count": 3},
    )
    assert preview.status_code == 200, preview.text
    client.post(
        f"/api/diagnosis/sessions/{session['session']['id']}/confirm",
        json={"idempotency_key": f"mastery-confirm-{time.time_ns()}"},
    )
    after = client.get(f"/api/knowledge-points/{point_id}/mastery").json()["mastery"]
    assert after["state"] == before["state"]
    assert after["attempts_considered"] == before["attempts_considered"]


def test_learning_context_reports_direct_branch(client, tmp_path):
    source, pack = helpers.generate_pack(client, tmp_path, color=(46, 56, 66), count=3)
    context = client.get(f"/api/sources/{source['id']}/learning-context").json()
    assert context["direct_eligible"] is False
    points = _points(pack)
    plan = client.post(
        "/api/plans",
        json={
            "title": "旧计划",
            "sources": [{"source_id": source["id"]}],
            "goal": "继续复习",
            "question_count": 3,
            "points": [
                {"knowledge_point_id": point["id"], "display_name": point["name"], "suggested_question_count": 1}
                for point in points
            ],
        },
    ).json()
    version = plan["current_version_id"]
    assert client.post(f"/api/plans/{plan['id']}/versions/{version}/confirm").status_code == 200
    context = client.get(f"/api/sources/{source['id']}/learning-context").json()
    assert context["direct_eligible"] is True