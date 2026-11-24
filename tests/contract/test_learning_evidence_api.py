from __future__ import annotations

import time

from sqlalchemy import select

from app.domain.models import Attempt, KnowledgePoint, Question, QuestionKnowledgePoint
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def _selected(question_id: str, mode: str) -> int | None:
    with SessionLocal() as db:
        question = db.get(Question, question_id)
        if mode == "correct":
            return question.answer_index
        if mode == "wrong":
            return (question.answer_index + 1) % 4
    return None


def _submit(client, pack: dict, modes: list[str], key: str):
    answers = []
    for question, mode in zip(pack["questions"], modes):
        if mode == "unanswered":
            continue
        answers.append(
            {"question_id": question["id"], "selected_index": _selected(question["id"], mode)}
        )
    return client.post(
        f"/api/packs/{pack['id']}/submit",
        json={"idempotency_key": key, "answers": answers},
    )


def _points_for_packs(pack_ids: list[str]) -> list[KnowledgePoint]:
    with SessionLocal() as db:
        return db.execute(
            select(KnowledgePoint)
            .join(
                QuestionKnowledgePoint,
                QuestionKnowledgePoint.knowledge_point_id == KnowledgePoint.id,
            )
            .join(Question, Question.id == QuestionKnowledgePoint.question_id)
            .where(Question.pack_id.in_(pack_ids))
            .distinct()
        ).scalars().all()


def test_submit_records_three_results_once(client, tmp_path):
    _, pack = helpers.generate_pack(client, tmp_path, color=(10, 20, 30), count=3)
    assert pack["plan"]["linked"] is False
    key = f"evidence-{time.time_ns()}"
    response = _submit(client, pack, ["correct", "wrong", "unanswered"], key)
    assert response.status_code == 200, response.text
    score = response.json()["score"]
    assert (score["correct"], score["wrong"], score["unanswered"], score["total"]) == (1, 1, 1, 3)

    evidence = client.get(f"/api/packs/{pack['id']}/evidence").json()
    assert len(evidence["items"]) == 3
    assert sorted(item["result"] for item in evidence["items"]) == ["correct", "unanswered", "wrong"]
    assert {item["source_type"] for item in evidence["items"]} == {"practice"}
    assert len({item["submitted_at"] for item in evidence["items"]}) == 1
    assert all(item["source_id"] for item in evidence["items"])
    assert all(item["knowledge_point_id"] for item in evidence["items"])
    assert all(item["time_known"] for item in evidence["items"])

    repeat = _submit(client, pack, ["correct", "wrong", "unanswered"], key)
    assert repeat.status_code == 200
    assert repeat.json()["duplicate"] is True
    again = client.get(f"/api/packs/{pack['id']}/evidence").json()
    assert len(again["items"]) == 3


def test_same_point_two_wrong_one_correct_counts_one_attempt(client, tmp_path):
    _, pack = helpers.generate_pack(client, tmp_path, color=(40, 50, 60), count=3)
    with SessionLocal() as db:
        rows = db.execute(
            select(QuestionKnowledgePoint).where(
                QuestionKnowledgePoint.question_id.in_([item["id"] for item in pack["questions"]])
            )
        ).scalars().all()
        point_id = rows[0].knowledge_point_id
        for row in rows:
            db.delete(row)
        db.flush()
        for item in pack["questions"]:
            db.add(
                QuestionKnowledgePoint(
                    question_id=item["id"],
                    knowledge_point_id=point_id,
                    role="primary",
                    reliability="confirmed",
                )
            )
        db.commit()

    response = _submit(client, pack, ["wrong", "wrong", "correct"], f"same-{time.time_ns()}")
    assert response.status_code == 200, response.text
    summary = client.get(f"/api/knowledge-points/{point_id}/evidence").json()["summary"]
    assert (summary["correct"], summary["wrong"], summary["unanswered"]) == (1, 2, 0)
    assert summary["attempt_count"] == 1
    assert summary["evidence_count"] == 3
    assert summary["mastery_status"] == "needs_reinforce"


def test_rename_keeps_evidence_and_counts(client, tmp_path):
    _, pack = helpers.generate_pack(client, tmp_path, color=(70, 80, 90), count=3)
    assert _submit(client, pack, ["correct", "wrong", "unanswered"], f"rename-{time.time_ns()}").status_code == 200
    with SessionLocal() as db:
        link = db.execute(
            select(QuestionKnowledgePoint).where(
                QuestionKnowledgePoint.question_id == pack["questions"][0]["id"]
            )
        ).scalars().first()
        point_id = link.knowledge_point_id
    before = client.get(f"/api/knowledge-points/{point_id}/evidence").json()

    renamed = client.put(
        f"/api/knowledge-points/{point_id}",
        json={"display_name": "根据子网掩码判断是否同网段"},
    )
    assert renamed.status_code == 200, renamed.text
    after = client.get(f"/api/knowledge-points/{point_id}/evidence").json()
    assert after["knowledge_point"]["id"] == point_id
    assert after["knowledge_point"]["display_name"] == "根据子网掩码判断是否同网段"
    assert after["summary"]["correct"] == before["summary"]["correct"]
    assert after["summary"]["wrong"] == before["summary"]["wrong"]
    assert after["summary"]["unanswered"] == before["summary"]["unanswered"]
    assert after["summary"]["evidence_count"] == before["summary"]["evidence_count"]


def test_same_name_points_from_two_sources_stay_separate(client, tmp_path):
    source_a = helpers.upload(client, helpers.make_png(tmp_path / "a.png", (20, 120, 80)))
    source_b = helpers.upload(client, helpers.make_png(tmp_path / "b.png", (200, 40, 40)))
    packs = []
    for index, source in enumerate([source_a, source_b]):
        created = helpers.create_pack(client, source["id"], count=3)
        assert created.status_code == 202, created.text
        job = helpers.wait_job(client, created.json()["job_id"])
        assert job["status"] == "succeeded", job
        pack = client.get(f"/api/packs/{job['pack_id']}").json()
        submitted = _submit(client, pack, ["correct", "wrong", "unanswered"], f"sep-{index}-{time.time_ns()}")
        assert submitted.status_code == 200, submitted.text
        packs.append(pack)

    points = _points_for_packs([item["id"] for item in packs])
    by_source: dict[str, set[str]] = {}
    for point in points:
        by_source.setdefault(point.source_id, set()).add(point.id)
    assert set(by_source) == {source_a["id"], source_b["id"]}
    assert by_source[source_a["id"]].isdisjoint(by_source[source_b["id"]])

    for point in points:
        summary = client.get(f"/api/knowledge-points/{point.id}/evidence").json()
        assert summary["summary"]["evidence_count"] <= 3
        assert {item["source_id"] for item in summary["items"]} == {point.source_id}


def test_retry_evidence_and_original_question_redo_are_distinct(client, tmp_path):
    source, parent = helpers.generate_pack(client, tmp_path, color=(100, 110, 120), count=3)
    assert _submit(client, parent, ["wrong", "wrong", "wrong"], f"parent-{time.time_ns()}").status_code == 200
    focus = client.get(f"/api/packs/{parent['id']}/review-focus").json()
    assert focus["available"] is True
    focus_text = [item["knowledge_point"] for item in focus["items"]]
    parent_points = {item["knowledge_point_id"] for item in focus["items"] if item["knowledge_point_id"]}
    assert parent_points
    parent_summary = client.get(
        f"/api/knowledge-points/{sorted(parent_points)[0]}/evidence"
    ).json()
    assert parent_summary["summary"]["last_retry"] is None

    created = helpers.create_pack(
        client,
        source["id"],
        count=3,
        parent_pack_id=parent["id"],
        review_focus=focus_text,
    )
    assert created.status_code == 202, created.text
    job = helpers.wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job
    child = client.get(f"/api/packs/{job['pack_id']}").json()
    retry_submit = _submit(client, child, ["wrong", "wrong", "wrong"], f"child-{time.time_ns()}")
    assert retry_submit.status_code == 200, retry_submit.text
    child_evidence = client.get(f"/api/packs/{job['pack_id']}/evidence").json()
    assert {item["source_type"] for item in child_evidence["items"]} == {"retry"}

    child_points = {item["knowledge_point_id"] for item in child_evidence["items"]}
    shared = child_points & parent_points
    assert shared
    summary = client.get(f"/api/knowledge-points/{sorted(shared)[0]}/evidence").json()
    assert summary["summary"]["last_retry"] is not None
    assert summary["summary"]["last_retry"]["attempt_id"] == retry_submit.json()["attempt_id"]
    assert summary["summary"]["last_retry"]["result"] == "wrong"

    child_reopened = client.post(f"/api/packs/{job['pack_id']}/reattempt")
    assert child_reopened.status_code == 201, child_reopened.text
    improved = _submit(client, child, ["correct", "correct", "correct"], f"improved-{time.time_ns()}")
    assert improved.status_code == 200, improved.text
    improved_summary = client.get(f"/api/knowledge-points/{sorted(shared)[0]}/evidence").json()
    assert improved_summary["summary"]["last_retry"]["attempt_id"] == improved.json()["attempt_id"]
    assert improved_summary["summary"]["last_retry"]["result"] == "correct"

    reopened = client.post(f"/api/packs/{parent['id']}/reattempt")
    assert reopened.status_code == 201, reopened.text
    redo = _submit(client, parent, ["correct", "correct", "correct"], f"redo-{time.time_ns()}")
    assert redo.status_code == 200, redo.text
    assert redo.json()["attempt_id"] == reopened.json()["attempt_id"]
    redo_evidence = client.get(f"/api/packs/{parent['id']}/evidence").json()
    assert {item["source_type"] for item in redo_evidence["items"]} == {"practice"}
    redo_items = [
        item for item in redo_evidence["items"] if item["attempt_id"] == redo.json()["attempt_id"]
    ]
    assert len(redo_items) == 3
    assert len(redo_evidence["items"]) >= 6
    with SessionLocal() as db:
        attempts = db.execute(select(Attempt).where(Attempt.pack_id == parent["id"])).scalars().all()
        submitted = [item for item in attempts if item.status == "submitted"]
    assert len(submitted) >= 2

def test_evidence_and_plan_survive_reinit(client, tmp_path):
    from app.infra.db import init_db

    source, pack = helpers.generate_pack(client, tmp_path, color=(130, 140, 150), count=3)
    submitted = _submit(client, pack, ["correct", "wrong", "unanswered"], f"persist-{time.time_ns()}")
    assert submitted.status_code == 200, submitted.text
    points = _points_for_packs([pack["id"]])
    point_id = points[0].id

    plan = client.post(
        "/api/plans",
        json={
            "title": "持久化计划",
            "sources": [{"source_id": source["id"], "page_start": None, "page_end": None}],
            "goal": "巩固同网段判断",
        },
    ).json()
    version_id = plan["current_version_id"]
    assert client.post(f"/api/plans/{plan['id']}/versions/{version_id}/confirm").status_code == 200

    before = client.get(f"/api/knowledge-points/{point_id}/evidence").json()["summary"]
    health = client.get("/api/health").json()
    assert health["schema_version"] == "11"
    assert health["migration"]["migration_version"] == "11"

    init_db()

    after = client.get(f"/api/knowledge-points/{point_id}/evidence").json()["summary"]
    assert after["evidence_count"] == before["evidence_count"]
    assert after["correct"] == before["correct"]
    assert after["wrong"] == before["wrong"]
    assert after["unanswered"] == before["unanswered"]
    reloaded = client.get(f"/api/plans/{plan['id']}").json()
    assert reloaded["status"] == "confirmed"
    assert reloaded["current_version_id"] == version_id
    assert len(reloaded["versions"]) == 1
