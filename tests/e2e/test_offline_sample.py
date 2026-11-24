from __future__ import annotations

from sqlalchemy import func, select

from app.domain import offline
from app.domain.models import Attempt, LearningPack, SourceMaterial
from app.infra.db import SessionLocal


def counts() -> dict:
    with SessionLocal() as db:
        return {
            "packs": db.execute(select(func.count()).select_from(LearningPack)).scalar(),
            "attempts": db.execute(select(func.count()).select_from(Attempt)).scalar(),
            "sources": db.execute(select(func.count()).select_from(SourceMaterial)).scalar(),
        }


def seed_index() -> dict[str, dict]:
    seed = offline.load_seed()
    result = {item["id"]: item for item in seed["questions"]}
    result.update({item["id"]: item for item in seed.get("retry_pool", [])})
    return result


def answers_for(questions: list[dict], correct: bool = True) -> list[dict]:
    index = seed_index()
    payload = []
    for question in questions:
        answer = index[question["id"]]["answer_index"]
        payload.append(
            {
                "question_id": question["id"],
                "selected_index": answer if correct else (answer + 1) % 4,
            }
        )
    return payload


def test_offline_two_rounds_refresh_retry_and_reset(client):
    client.post("/api/offline/reset")
    before = counts()

    first = client.get("/api/offline/sample").json()
    assert first["submitted"] is False
    assert first["result"] is None
    for question in first["questions"]:
        assert "answer_index" not in question
        assert "explanation" not in question

    round1 = client.post(
        "/api/offline/attempts", json={"answers": answers_for(first["questions"], correct=False)}
    )
    assert round1.status_code == 200
    result1 = round1.json()
    assert result1["completed"] is False
    assert result1["wrong_knowledge_points"]
    assert all("answer_index" in item for item in result1["questions"])

    # 刷新/重新打开已交卷轮次：服务端返回本轮结果与错题考点。
    refreshed = client.get("/api/offline/sample").json()
    assert refreshed["submitted"] is True
    assert refreshed["result"] is not None
    assert refreshed["result"]["score"] == result1["score"]
    assert refreshed["result"]["wrong_knowledge_points"] == result1["wrong_knowledge_points"]
    assert all("explanation" in item for item in refreshed["result"]["questions"])

    retry = client.post(
        "/api/offline/retry",
        json={"focus_ids": refreshed["result"]["wrong_knowledge_points"]},
    )
    assert retry.status_code == 200
    round2 = retry.json()
    assert round2["round_no"] == 2
    assert round2["questions"]

    # 第二轮可提交，并在全部答对后给出完成反馈。
    second = client.post(
        "/api/offline/attempts", json={"answers": answers_for(round2["questions"], correct=True)}
    )
    assert second.status_code == 200
    result2 = second.json()
    assert result2["completed"] is True
    assert result2["completed_text"]
    assert result2["next_round_suggestion"]["available"] is False

    reopened = client.get("/api/offline/sample").json()
    assert reopened["submitted"] is True
    assert reopened["result"]["completed"] is True
    assert reopened["completed"] is True

    assert counts() == before
    client.post("/api/offline/reset")


def test_offline_reset_allows_submitting_again(client):
    client.post("/api/offline/reset")
    sample = client.get("/api/offline/sample").json()
    first = client.post(
        "/api/offline/attempts", json={"answers": answers_for(sample["questions"], correct=True)}
    )
    assert first.status_code == 200
    assert first.json()["completed"] is True

    reset = client.post("/api/offline/reset")
    assert reset.status_code == 200
    after_reset = client.get("/api/offline/sample").json()
    assert after_reset["submitted"] is False
    assert after_reset["result"] is None

    again = client.post(
        "/api/offline/attempts",
        json={"answers": answers_for(after_reset["questions"], correct=True)},
    )
    assert again.status_code == 200
    assert again.json()["completed"] is True


def test_offline_retry_rejects_focus_outside_current_round(client):
    client.post("/api/offline/reset")
    sample = client.get("/api/offline/sample").json()
    client.post(
        "/api/offline/attempts", json={"answers": answers_for(sample["questions"], correct=False)}
    )
    stale = client.post(
        "/api/offline/retry", json={"focus_ids": ["不属于本轮的考点"]}
    )
    assert stale.status_code == 422
    assert stale.json()["error"]["code"] == "focus_mismatch"
    client.post("/api/offline/reset")