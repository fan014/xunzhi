from __future__ import annotations

import json
from types import SimpleNamespace

from app.domain import assessment as assessment_service
from app.infra.model_client import FakeModelClient
from tests import stage4_helpers as helpers


class SpyAssessmentClient:
    def __init__(self, evidence_ids):
        self.evidence_ids = evidence_ids
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        raw = request.task_text.split("【测评输入】\n", 1)[1]
        data, _ = json.JSONDecoder().raw_decode(raw.strip())
        point_id = data["points"][0]["knowledge_point_id"]
        return SimpleNamespace(
            content_text=json.dumps(
                {
                    "findings": [
                        {
                            "knowledge_point_id": point_id,
                            "kind": "observation",
                            "judgement": "最近一次练习在该考点答错。",
                            "evidence_ids": self.evidence_ids,
                            "note": "测试越界证据。",
                        }
                    ],
                    "route_steps": [],
                    "unknowns": [],
                },
                ensure_ascii=False,
            ),
            finish_reason="stop",
            usage={},
        )


def _submit_and_assess(client, pack):
    submitted = helpers.submit_wrong_once(client, pack)
    response = client.post(
        f"/api/packs/{pack['id']}/assessment",
        json={"attempt_id": submitted["attempt_id"]},
    )
    assert response.status_code == 200, response.text
    return submitted, response.json()


def test_assessment_ready_reused_and_visible(client, tmp_path):
    source, pack = helpers.generate_pack(client, tmp_path, color=(31, 131, 71), count=3)
    submitted, body = _submit_and_assess(client, pack)
    assert body["status"] == "ready"
    assert body["findings"] or body["route_steps"]
    assert body["evidence_ids"]
    assert body["attempt_id"] == submitted["attempt_id"]
    assert body["rule_version"] == assessment_service.PROFILE_RULE_VERSION

    again = client.post(
        f"/api/packs/{pack['id']}/assessment",
        json={"attempt_id": submitted["attempt_id"]},
    ).json()
    assert again["id"] == body["id"]

    detail = client.get(f"/api/packs/{pack['id']}").json()
    assert detail["result"]["assessment"]["id"] == body["id"]
    profile = client.get(f"/api/learner-profile?source_id={source['id']}").json()
    assert profile["items"]
    assert profile["items"][0]["id"] == body["id"]
    assert profile["items"][0]["evidence"]


def test_out_of_scope_evidence_fails_then_retry_succeeds(client, tmp_path, monkeypatch):
    _, pack = helpers.generate_pack(client, tmp_path, color=(32, 132, 72), count=3)
    submitted = helpers.submit_wrong_once(client, pack)
    bad = SpyAssessmentClient(["learning_evidence_not_in_input"])
    monkeypatch.setattr(assessment_service, "get_model_client", lambda: bad)
    failed = client.post(
        f"/api/packs/{pack['id']}/assessment",
        json={"attempt_id": submitted["attempt_id"]},
    ).json()
    assert failed["status"] == "failed"
    assert failed["failure"]["code"] == "assessment_out_of_scope"
    assert failed["retryable"] is True

    monkeypatch.setattr(assessment_service, "get_model_client", lambda: FakeModelClient())
    retried = client.post(
        f"/api/packs/{pack['id']}/assessment",
        json={"attempt_id": submitted["attempt_id"], "force": True},
    ).json()
    assert retried["status"] == "ready"
    assert retried["id"] != failed["id"]


def test_previous_ready_snapshot_survives_failed_retry(client, tmp_path, monkeypatch):
    _, pack = helpers.generate_pack(client, tmp_path, color=(33, 133, 73), count=3)
    submitted, ready = _submit_and_assess(client, pack)
    monkeypatch.setattr(
        assessment_service,
        "get_model_client",
        lambda: SpyAssessmentClient(["learning_evidence_not_in_input"]),
    )
    failed = client.post(
        f"/api/packs/{pack['id']}/assessment",
        json={"attempt_id": submitted["attempt_id"], "force": True},
    ).json()
    assert failed["status"] == "failed"

    from app.infra.db import SessionLocal
    from app.domain.models import LearnerProfileSnapshot

    with SessionLocal() as db:
        latest = assessment_service.latest_ready(db, ready["source_id"])
    assert latest is not None
    assert latest.id == ready["id"]


def test_new_attempt_invalidates_older_plan_basis(client, tmp_path):
    source, pack = helpers.generate_pack(client, tmp_path, color=(34, 134, 74), count=3)
    _, first = _submit_and_assess(client, pack)
    client.post(f"/api/packs/{pack['id']}/reattempt")
    second_submitted = helpers.submit_wrong_once(client, pack)
    second = client.post(
        f"/api/packs/{pack['id']}/assessment",
        json={"attempt_id": second_submitted["attempt_id"]},
    ).json()
    assert second["status"] == "ready"
    assert second["id"] != first["id"]
    assert any(item["is_original_redo"] for item in second["evidence"])

    plan = client.post(
        f"/api/sources/{source['id']}/next-plan",
        json={"goal": "继续复习薄弱考点", "question_count": 5, "points": []},
    ).json()
    assert plan["assessment"]["id"] == second["id"]
    assert plan["version"]["assessment_id"] == second["id"]

def test_confirmed_plan_carries_assessment_into_generation(client, tmp_path):
    source, pack = helpers.generate_pack(client, tmp_path, color=(35, 135, 75), count=3)
    _, snapshot = _submit_and_assess(client, pack)
    plan_response = client.post(
        f"/api/sources/{source['id']}/next-plan",
        json={"goal": "继续复习薄弱考点", "question_count": 5, "points": []},
    )
    assert plan_response.status_code == 201, plan_response.text
    plan = plan_response.json()
    assert plan["assessment_applied"] is True
    assert plan["assessment_basis"] == "profile"
    assert plan["version"]["assessment_id"] == snapshot["id"]
    assert any(
        (item.get("assessment") or {}).get("judgement") for item in plan["adjustments"]
    )

    plan_id = plan["plan"]["id"]
    version = plan["version"]
    confirmed = client.post(f"/api/plans/{plan_id}/versions/{version['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    created = client.post(
        "/api/packs",
        json={
            "idempotency_key": f"profile-pack-{helpers.time.time_ns()}",
            "sources": [{"source_id": source["id"], "page_start": None, "page_end": None}],
            "progress_text": helpers.PROGRESS,
            "difficulty": "consolidate",
            "question_count": version["question_count"],
            "consent_ack": True,
            "plan_version_id": version["id"],
        },
    )
    assert created.status_code == 202, created.text
    job = helpers.wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    detail = client.get(f"/api/packs/{job['pack_id']}").json()
    assert detail["plan"]["assessment"]["id"] == snapshot["id"]
    assert detail["plan"]["assessment"]["route_steps"] or detail["plan"]["assessment"]["findings"]