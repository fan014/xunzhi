from __future__ import annotations

import time

from sqlalchemy import select

from app.domain.models import KnowledgePoint, Question, QuestionKnowledgePoint
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def _points_for_pack(pack: dict) -> dict[str, dict]:
    ids = [item["id"] for item in pack["questions"]]
    with SessionLocal() as db:
        links = db.execute(
            select(QuestionKnowledgePoint).where(QuestionKnowledgePoint.question_id.in_(ids))
        ).scalars().all()
        result = {}
        for link in links:
            point = db.get(KnowledgePoint, link.knowledge_point_id)
            result[link.question_id] = {
                "id": point.id,
                "name": point.display_name,
            }
    return result


def _create_plan(client, source_id: str, points: list[dict], count: int, goal: str):
    body = {
        "title": "复习计划",
        "sources": [{"source_id": source_id, "page_start": None, "page_end": None}],
        "learned": ["交换机转发"],
        "unlearned": ["路由协议"],
        "goal": goal,
        "question_count": count,
        "points": [
            {
                "knowledge_point_id": point["id"],
                "display_name": point["name"],
                "priority": "review",
                "suggested_question_count": count // max(len(points), 1),
            }
            for point in points
        ],
    }
    created = client.post("/api/plans", json=body)
    assert created.status_code == 201, created.text
    plan = created.json()
    version_id = plan["current_version_id"]
    confirmed = client.post(f"/api/plans/{plan['id']}/versions/{version_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return version_id


def _create_pack(client, source_id: str, plan_version_id: str, count: int) -> dict:
    body = {
        "idempotency_key": f"stage2-pack-{time.time_ns()}",
        "sources": [{"source_id": source_id, "page_start": None, "page_end": None}],
        "progress_text": helpers.PROGRESS,
        "difficulty": "consolidate",
        "question_count": count,
        "consent_ack": True,
        "plan_version_id": plan_version_id,
    }
    created = client.post("/api/packs", json=body)
    assert created.status_code == 202, created.text
    job = helpers.wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job
    return client.get(f"/api/packs/{job['pack_id']}").json()


def _answer(question_id: str, mode: str | None) -> dict | None:
    if mode is None:
        return None
    with SessionLocal() as db:
        question = db.get(Question, question_id)
        index = question.answer_index if mode == "correct" else (question.answer_index + 1) % 4
    return {"question_id": question_id, "selected_index": index}


def _submit(client, pack: dict, modes: dict[str, str], key: str):
    answers = []
    for question in pack["questions"]:
        item = _answer(question["id"], modes.get(question["id"]))
        if item is not None:
            answers.append(item)
    return client.post(
        f"/api/packs/{pack['id']}/submit",
        json={"idempotency_key": key, "answers": answers},
    )


def _mastery(client, point_id: str) -> dict:
    return client.get(f"/api/knowledge-points/{point_id}/mastery").json()["mastery"]


def test_stage2_scenario_a_full_flow(client, tmp_path):
    source, base = helpers.generate_pack(client, tmp_path, color=(21, 42, 63), count=3)
    point_by_question = _points_for_pack(base)
    points = list(point_by_question.values())
    assert len({item["id"] for item in points}) == 3

    for point in points:
        assert _mastery(client, point["id"])["state"] == "pending"

    v1 = _create_plan(client, source["id"], points, 3, "先复习同网段、交换机和默认网关")
    pack_v1 = _create_pack(client, source["id"], v1, 3)
    assert pack_v1["plan"]["version_id"] == v1

    v1_points = _points_for_pack(pack_v1)
    q1, q2, q3 = pack_v1["questions"]
    p1 = v1_points[q1["id"]]
    p2 = v1_points[q2["id"]]
    p3 = v1_points[q3["id"]]
    submitted = _submit(
        client,
        pack_v1,
        {q1["id"]: "wrong", q2["id"]: "correct"},
        f"stage2-submit-{time.time_ns()}",
    )
    assert submitted.status_code == 200, submitted.text

    assert _mastery(client, p1["id"])["state"] == "needs_reinforce"
    assert _mastery(client, p2["id"])["state"] == "consolidating"
    assert _mastery(client, p3["id"])["state"] == "pending"

    changes = client.get(
        f"/api/packs/{pack_v1['id']}/mastery-changes", params={"attempt_id": submitted.json()["attempt_id"]}
    ).json()
    assert {item["knowledge_point_id"] for item in changes["changes"]} == {p1["id"], p2["id"]}
    assert any(item["knowledge_point_id"] == p3["id"] for item in changes["unanswered_points"])

    suggested = client.post(
        f"/api/sources/{source['id']}/next-plan",
        json={
            "goal": "优先补同网段判断",
            "question_count": 5,
            "points": [{"knowledge_point_id": point["id"], "display_name": point["name"]} for point in points],
        },
    )
    assert suggested.status_code == 201, suggested.text
    draft = suggested.json()
    items = {item["knowledge_point_id"]: item["suggested_question_count"] for item in draft["version"]["items"]}
    assert sum(items.values()) == 5
    assert items[p1["id"]] >= 2
    assert any(
        item["knowledge_point_id"] == p1["id"]
        and item["current_question_count"] > item["previous_question_count"]
        for item in draft["version"]["adjustment"]
    )
    v2 = draft["version"]["id"]
    assert client.post(f"/api/plans/{draft['plan']['id']}/versions/{v2}/confirm").status_code == 200

    pack_v2 = _create_pack(client, source["id"], v2, 5)
    assert pack_v2["plan"]["version_id"] == v2
    assert pack_v2["plan"]["delivery"]["expected"]["total"] == 5
    assert client.get(f"/api/packs/{pack_v1['id']}").json()["plan"]["version_id"] == v1

    reopened = client.post(f"/api/packs/{pack_v1['id']}/reattempt")
    assert reopened.status_code == 201, reopened.text
    corrected = _submit(client, pack_v1, {q1["id"]: "correct"}, f"stage2-redo-{time.time_ns()}")
    assert corrected.status_code == 200, corrected.text
    corrected_state = _mastery(client, p1["id"])
    assert corrected_state["state"] == "consolidating"
    assert "corrected_pending_new" in corrected_state["flags"]

    v2_points = _points_for_pack(pack_v2)
    new_question = next(
        item["id"]
        for item in pack_v2["questions"]
        if v2_points[item["id"]]["id"] == p1["id"] and item["ordinal"] >= 4
    )
    verified = _submit(client, pack_v2, {new_question: "correct"}, f"stage2-new-{time.time_ns()}")
    assert verified.status_code == 200, verified.text
    stable_state = _mastery(client, p1["id"])
    assert stable_state["state"] == "stable", stable_state
    assert stable_state["new_question_since_previous"] is True

    reopened_v2 = client.post(f"/api/packs/{pack_v2['id']}/reattempt")
    assert reopened_v2.status_code == 201, reopened_v2.text
    regressed = _submit(client, pack_v2, {new_question: "wrong"}, f"stage2-regress-{time.time_ns()}")
    assert regressed.status_code == 200, regressed.text
    assert _mastery(client, p1["id"])["state"] == "needs_reinforce"


def test_plan_delivery_shortfall_and_question_count_mismatch(client, tmp_path):
    source, base = helpers.generate_pack(client, tmp_path, color=(31, 41, 51), count=3)
    point_by_question = _points_for_pack(base)
    points = list(point_by_question.values())
    assert len(points) == 3

    body = {
        "title": "差异计划",
        "sources": [{"source_id": source["id"]}],
        "goal": "验证交付差异",
        "question_count": 5,
        "points": [
            {"knowledge_point_id": points[0]["id"], "display_name": points[0]["name"], "suggested_question_count": 2},
            {"knowledge_point_id": points[1]["id"], "display_name": points[1]["name"], "suggested_question_count": 1},
            {"knowledge_point_id": points[2]["id"], "display_name": points[2]["name"], "suggested_question_count": 2},
        ],
    }
    plan = client.post("/api/plans", json=body).json()
    version_id = plan["current_version_id"]
    assert client.post(f"/api/plans/{plan['id']}/versions/{version_id}/confirm").status_code == 200

    mismatch = client.post(
        "/api/packs",
        json={
            "idempotency_key": f"mismatch-{time.time_ns()}",
            "sources": [{"source_id": source["id"]}],
            "progress_text": helpers.PROGRESS,
            "difficulty": "consolidate",
            "question_count": 3,
            "consent_ack": True,
            "plan_version_id": version_id,
        },
    )
    assert mismatch.status_code == 422, mismatch.text
    assert mismatch.json()["error"]["code"] == "plan_question_count_mismatch"

    pack = _create_pack(client, source["id"], version_id, 5)
    delivery = pack["plan"]["delivery"]
    assert delivery["expected"]["total"] == 5
    assert delivery["actual"]["total"] == 5
    assert delivery["shortfall"] or delivery["extra"]
    assert delivery["retry_available"] is True


def test_next_plan_is_idempotent_until_evidence_changes(client, tmp_path):
    source, base = helpers.generate_pack(client, tmp_path, color=(61, 71, 81), count=3)
    points = list(_points_for_pack(base).values())
    body = {
        "goal": "保持同一输入",
        "question_count": 3,
        "points": [{"knowledge_point_id": point["id"], "display_name": point["name"]} for point in points],
    }
    first = client.post(f"/api/sources/{source['id']}/next-plan", json=body)
    assert first.status_code == 201, first.text
    second = client.post(f"/api/sources/{source['id']}/next-plan", json=body)
    assert second.status_code == 201, second.text
    assert second.json()["reused"] is True
    assert second.json()["version"]["id"] == first.json()["version"]["id"]


def test_learning_context_lists_three_points(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(91, 101, 111), count=3)
    context = client.get(f"/api/sources/{source['id']}/learning-context")
    assert context.status_code == 200, context.text
    payload = context.json()
    assert payload["can_continue"] is True
    assert len(payload["mastery"]) == 3
    assert {item["mastery"]["state"] for item in payload["mastery"]} == {"pending"}

def test_delivery_rejects_short_total_and_reports_point_shortfall():
    from types import SimpleNamespace

    import pytest

    from app.domain.generation import validate_plan_delivery
    from app.domain.validation import ValidationError

    output = SimpleNamespace(questions=[SimpleNamespace(ordinal=i) for i in range(1, 4)])
    short = {"items": [{"knowledge_point_id": "kp1", "suggested_question_count": 5}], "total_question_count": 5}
    with pytest.raises(ValidationError):
        validate_plan_delivery(output, {1: "kp1", 2: "kp1", 3: "kp1"}, short)

    constraint = {
        "items": [
            {"knowledge_point_id": "kp1", "suggested_question_count": 2},
            {"knowledge_point_id": "kp2", "suggested_question_count": 1},
        ],
        "total_question_count": 3,
    }
    delivery = validate_plan_delivery(output, {1: "kp1", 2: "kp2", 3: "kp2"}, constraint)
    assert delivery["shortfall"][0]["knowledge_point_id"] == "kp1"
    assert delivery["shortfall"][0]["missing"] == 1
    assert delivery["retry_available"] is True