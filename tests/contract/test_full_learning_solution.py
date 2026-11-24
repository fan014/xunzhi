from __future__ import annotations

from app.domain.knowledge import ensure_knowledge_point
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def _add_points(source_id: str, names: list[str]) -> None:
    with SessionLocal() as db:
        for name in names:
            ensure_knowledge_point(db, source_id, name)
        db.commit()


def test_auto_plan_builds_modules_and_can_exceed_eight_questions(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(61, 91, 121), count=3)
    _add_points(
        source["id"],
        ["同网段判断", "跨网段转发", "默认网关", "MAC 地址转发", "设备角色对比"],
    )
    response = client.post(
        f"/api/sources/{source['id']}/next-plan",
        json={"goal": "完整学习这份资料", "learning_mode": "auto", "points": []},
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    version = plan["version"]
    assert version["question_count"] > 8, version["question_count"]
    assert len(version["modules"]) >= 3
    assert sum(item["suggested_question_count"] for item in version["modules"]) == version["question_count"]
    assert all(module["objective"] and module["completion_standard"] and module["rationale"] for module in version["modules"])

    confirmed = client.post(
        f"/api/plans/{plan['plan']['id']}/versions/{version['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    created = client.post(
        "/api/packs",
        json={
            "idempotency_key": f"auto-solution-{helpers.time.time_ns()}",
            "sources": [{"source_id": source["id"], "page_start": None, "page_end": None}],
            "progress_text": helpers.PROGRESS,
            "difficulty": "consolidate",
            "consent_ack": True,
            "plan_version_id": version["id"],
        },
    )
    assert created.status_code == 202, created.text
    job = helpers.wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")

    detail = client.get(f"/api/packs/{job['pack_id']}").json()
    assert detail["question_count"] == version["question_count"]
    assert len(detail["questions"]) == version["question_count"]
    assert len(detail["modules"]) == len(version["modules"])
    for module in detail["modules"]:
        assert module["handout"], module["module_id"]
        assert module["questions"], module["module_id"]
        assert module["rationale"]
    assert detail["review"]["questions"]
    assert detail["plan"]["delivery"]["module_shortfall"] == []
    assert detail["plan"]["delivery"]["shortfall"] == []


def test_short_source_auto_plan_stays_compact(client, tmp_path):
    source, _ = helpers.generate_pack(client, tmp_path, color=(62, 92, 122), count=3)
    response = client.post(
        f"/api/sources/{source['id']}/next-plan",
        json={"goal": "短资料复习", "learning_mode": "auto", "points": []},
    )
    assert response.status_code == 201, response.text
    version = response.json()["version"]
    assert len(version["modules"]) == 1
    assert version["question_count"] <= 8
    assert sum(item["suggested_question_count"] for item in version["items"]) == version["question_count"]

def test_auto_diagnosis_without_prior_pack_creates_one_module_solution(client, tmp_path):
    note = tmp_path / "fresh.txt"
    note.write_text(
        "同一网段内的主机通过交换机转发帧，跨网段通信需要经过默认网关和路由器。",
        encoding="utf-8",
    )
    source = helpers.upload(client, note)
    started = client.post(
        f"/api/sources/{source['id']}/diagnosis/sessions",
        json={
            "idempotency_key": f"auto-dx-{helpers.time.time_ns()}",
            "goal": "同网段与跨网段转发",
            "learning_mode": "focus",
        },
    )
    assert started.status_code == 201, started.text
    session = started.json()["session"]
    for _ in range(6):
        question = session.get("current_question")
        if not question:
            break
        answered = client.post(
            f"/api/diagnosis/sessions/{session['id']}/answers",
            json={
                "idempotency_key": f"auto-dx-answer-{helpers.time.time_ns()}",
                "question_id": question["id"],
                "selected_index": 1,
                "status": "answered",
            },
        )
        assert answered.status_code == 200, answered.text
        session = answered.json()

    preview = client.post(
        f"/api/diagnosis/sessions/{session['id']}/finish",
        json={"goal": "同网段与跨网段转发", "learning_mode": "focus"},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    version = body["version"]
    assert version["modules"], body
    assert version["question_count"] >= 1
    confirmed = client.post(
        f"/api/diagnosis/sessions/{session['id']}/confirm",
        json={"idempotency_key": f"auto-dx-confirm-{helpers.time.time_ns()}"},
    )
    assert confirmed.status_code == 200, confirmed.text
    job = helpers.wait_job(client, confirmed.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    detail = client.get(f"/api/packs/{job['pack_id']}").json()
    assert detail["modules"]
    assert detail["questions"]
    assert detail["modules"][0]["handout"]