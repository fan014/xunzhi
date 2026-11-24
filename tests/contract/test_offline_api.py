from __future__ import annotations

from sqlalchemy import func, select

from app.domain import offline
from app.domain.models import (
    Attempt,
    KnowledgePoint,
    LearningEvidence,
    LearningPack,
    LearningPlan,
    LearningPlanVersion,
    PackPlanVersion,
    SourceMaterial,
)
from app.infra.db import SessionLocal


def table_counts() -> dict:
    with SessionLocal() as db:
        return {
            "packs": db.execute(select(func.count()).select_from(LearningPack)).scalar(),
            "attempts": db.execute(select(func.count()).select_from(Attempt)).scalar(),
            "sources": db.execute(select(func.count()).select_from(SourceMaterial)).scalar(),
            "points": db.execute(select(func.count()).select_from(KnowledgePoint)).scalar(),
            "evidence": db.execute(select(func.count()).select_from(LearningEvidence)).scalar(),
            "plans": db.execute(select(func.count()).select_from(LearningPlan)).scalar(),
            "plan_versions": db.execute(
                select(func.count()).select_from(LearningPlanVersion)
            ).scalar(),
            "adoptions": db.execute(select(func.count()).select_from(PackPlanVersion)).scalar(),
        }


def test_offline_sample_never_leaks_answers(client):
    client.post("/api/offline/reset")
    payload = client.get("/api/offline/sample").json()
    assert payload["is_offline"] is True
    assert payload["submitted"] is False
    for question in payload["questions"]:
        assert "answer_index" not in question
        assert "explanation" not in question
        assert len(question["options"]) == 4


def test_offline_flow_is_isolated_from_real_records(client):
    client.post("/api/offline/reset")
    before = table_counts()
    sample = client.get("/api/offline/sample").json()
    seed = {item["id"]: item for item in offline.load_seed()["questions"]}
    answers = []
    for question in sample["questions"]:
        correct = seed[question["id"]]["answer_index"]
        answers.append({"question_id": question["id"], "selected_index": (correct + 1) % 4})
    result = client.post("/api/offline/attempts", json={"answers": answers})
    assert result.status_code == 200
    payload = result.json()
    assert payload["is_offline"] is True
    assert payload["wrong_knowledge_points"]

    duplicate = client.post("/api/offline/attempts", json={"answers": answers})
    assert duplicate.status_code == 409

    retry = client.post(
        "/api/offline/retry", json={"focus_ids": payload["wrong_knowledge_points"]}
    )
    assert retry.status_code == 200
    assert retry.json()["round_no"] >= 2

    reset = client.post("/api/offline/reset")
    assert reset.status_code == 200
    assert table_counts() == before


def test_offline_retry_without_focus_is_rejected(client):
    client.post("/api/offline/reset")
    sample = client.get("/api/offline/sample").json()
    seed = {item["id"]: item for item in offline.load_seed()["questions"]}
    answers = [
        {"question_id": q["id"], "selected_index": seed[q["id"]]["answer_index"]}
        for q in sample["questions"]
    ]
    result = client.post("/api/offline/attempts", json={"answers": answers}).json()
    assert result["completed"] is True
    retry = client.post("/api/offline/retry", json={"focus_ids": []})
    assert retry.status_code == 422

def test_offline_sample_restores_saved_result_only_after_submit(client):
    client.post("/api/offline/reset")
    before_submit = client.get("/api/offline/sample").json()
    assert before_submit["result"] is None

    seed = {item["id"]: item for item in offline.load_seed()["questions"]}
    answers = [
        {"question_id": q["id"], "selected_index": (seed[q["id"]]["answer_index"] + 1) % 4}
        for q in before_submit["questions"]
    ]
    result = client.post("/api/offline/attempts", json={"answers": answers}).json()

    reopened = client.get("/api/offline/sample").json()
    assert reopened["submitted"] is True
    assert reopened["result"]["score"] == result["score"]
    assert reopened["result"]["wrong_knowledge_points"] == result["wrong_knowledge_points"]
    assert all("answer_index" in item for item in reopened["result"]["questions"])
    assert all("explanation" in item for item in reopened["result"]["questions"])
    assert all("answer_index" not in item for item in reopened["questions"])
    client.post("/api/offline/reset")


def test_offline_retry_rejects_focus_from_another_round(client):
    client.post("/api/offline/reset")
    sample = client.get("/api/offline/sample").json()
    seed = {item["id"]: item for item in offline.load_seed()["questions"]}
    answers = [
        {"question_id": q["id"], "selected_index": (seed[q["id"]]["answer_index"] + 1) % 4}
        for q in sample["questions"]
    ]
    client.post("/api/offline/attempts", json={"answers": answers})
    stale = client.post("/api/offline/retry", json={"focus_ids": ["不属于本轮的旧考点"]})
    assert stale.status_code == 422
    assert stale.json()["error"]["code"] == "focus_mismatch"
    client.post("/api/offline/reset")