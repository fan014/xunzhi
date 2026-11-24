from __future__ import annotations

import time

from tests import stage4_helpers as helpers


def _plan_body(source_id: str, goal: str = "掌握同网段判断"):
    return {
        "title": "复习计划",
        "sources": [{"source_id": source_id, "page_start": None, "page_end": None}],
        "learned": ["交换机转发"],
        "unlearned": ["路由协议"],
        "goal": goal,
        "points": [],
        "question_count": 3,
        "handout_advice": "先对照讲解同网段与跨网段",
    }


def _create_with_plan(client, source_id: str, plan_version_id: str):
    body = {
        "idempotency_key": f"plan-pack-{time.time_ns()}",
        "sources": [{"source_id": source_id, "page_start": None, "page_end": None}],
        "progress_text": helpers.PROGRESS,
        "difficulty": "consolidate",
        "question_count": 3,
        "consent_ack": True,
        "plan_version_id": plan_version_id,
    }
    return client.post("/api/packs", json=body)


def _wait_pack(client, response):
    assert response.status_code == 202, response.text
    job = helpers.wait_job(client, response.json()["job_id"])
    assert job["status"] == "succeeded", job
    return client.get(f"/api/packs/{job['pack_id']}").json()


def test_plan_draft_and_confirm_validation(client):
    created = client.post("/api/plans", json={"title": "草稿", "sources": [], "goal": ""})
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["status"] == "draft"
    version_id = plan["current_version_id"]

    rejected = client.post(f"/api/plans/{plan['id']}/versions/{version_id}/confirm")
    assert rejected.status_code == 422, rejected.text
    error = rejected.json()["error"]
    assert error["code"] == "plan_incomplete"
    assert "sources" in error["missing"]
    assert "goal" in error["missing"]

    reloaded = client.get(f"/api/plans/{plan['id']}").json()
    assert reloaded["status"] == "draft"
    assert reloaded["versions"][0]["status"] == "draft"


def test_plan_versions_and_pack_adoption(client, tmp_path):
    source = helpers.upload(client, helpers.make_png(tmp_path / "plan.png"))
    created = client.post("/api/plans", json=_plan_body(source["id"], "第一版目标"))
    assert created.status_code == 201, created.text
    plan = created.json()
    v1 = plan["current_version_id"]
    confirmed = client.post(f"/api/plans/{plan['id']}/versions/{v1}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"

    first = _wait_pack(client, _create_with_plan(client, source["id"], v1))
    assert first["plan"]["linked"] is True
    assert first["plan"]["version_id"] == v1

    updated = client.post(
        f"/api/plans/{plan['id']}/versions",
        json=_plan_body(source["id"], "第二版目标：增加跨网段对照"),
    )
    assert updated.status_code == 201, updated.text
    v2 = updated.json()["current_version_id"]
    assert v2 != v1
    assert updated.json()["status"] == "draft"
    assert {item["id"] for item in updated.json()["versions"]} == {v1, v2}

    rejected = _create_with_plan(client, source["id"], v2)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "plan_version_not_confirmed"

    assert client.post(f"/api/plans/{plan['id']}/versions/{v2}/confirm").status_code == 200
    again = client.get(f"/api/packs/{first['id']}").json()
    assert again["plan"]["version_id"] == v1

    second = _wait_pack(client, _create_with_plan(client, source["id"], v2))
    assert second["plan"]["version_id"] == v2
    third = _wait_pack(client, _create_with_plan(client, source["id"], v1))
    assert third["plan"]["version_id"] == v1

    history = client.get(f"/api/plans/{plan['id']}").json()
    statuses = {item["id"]: item["status"] for item in history["versions"]}
    assert statuses[v1] == "confirmed"
    assert statuses[v2] == "confirmed"


def test_plan_version_source_mismatch_rejected(client, tmp_path):
    source_a = helpers.upload(client, helpers.make_png(tmp_path / "a.png", (20, 120, 80)))
    source_b = helpers.upload(client, helpers.make_png(tmp_path / "b.png", (200, 40, 40)))
    created = client.post("/api/plans", json=_plan_body(source_a["id"]))
    plan = created.json()
    version_id = plan["current_version_id"]
    assert client.post(f"/api/plans/{plan['id']}/versions/{version_id}/confirm").status_code == 200

    mismatch = _create_with_plan(client, source_b["id"], version_id)
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["error"]["code"] == "plan_version_mismatch"


def test_plan_can_confirm_with_empty_optional_fields(client, tmp_path):
    source = helpers.upload(client, helpers.make_png(tmp_path / "optional.png"))
    body = {
        "title": "极简计划",
        "sources": [{"source_id": source["id"]}],
        "learned": [],
        "unlearned": [],
        "goal": "先做一轮巩固",
        "points": [],
    }
    created = client.post("/api/plans", json=body)
    plan = created.json()
    version_id = plan["current_version_id"]
    confirmed = client.post(f"/api/plans/{plan['id']}/versions/{version_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"