from __future__ import annotations

import time


def upload(client, path, name=None):
    with open(path, "rb") as fh:
        files = {"file": (name or path.name, fh, "application/octet-stream")}
        response = client.post("/api/sources", files=files)
    assert response.status_code == 201, response.text
    return response.json()["source"]


def wait_job(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/jobs/{job_id}").json()
        if data["status"] in {"succeeded", "failed", "insufficient_evidence", "cancelled"}:
            return data
        time.sleep(0.2)
    raise AssertionError("job did not finish in time")


def test_png_slice_end_to_end(client, topology_png):
    source = upload(client, topology_png)
    assert source["kind"] == "image"
    assert source["page_count"] is None

    created = client.post(
        "/api/packs",
        json={
            "idempotency_key": "slice-png-0001",
            "sources": [{"source_id": source["id"], "page_start": None, "page_end": None}],
            "progress_text": "我学完了交换机转发，还没学路由协议。同一网段和跨网段发包容易混，想用这张图练一下。",
            "difficulty": "consolidate",
            "question_count": 3,
            "consent_ack": True,
            "parent_pack_id": None,
            "review_focus": [],
        },
    )
    assert created.status_code == 202, created.text
    job = wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job
    pack_id = job["pack_id"]

    detail = client.get(f"/api/packs/{pack_id}").json()
    assert detail["question_count"] == 3
    assert len(detail["questions"]) == 3
    assert detail["answer_revealed"] is False
    for question in detail["questions"]:
        assert "answer_index" not in question
        assert "explanation" not in question
        assert "citations" not in question
        assert len(question["options"]) == 4

    assert any(item["is_image_based"] for item in detail["questions"])
    assert any(item["section_type"] == "visual" for item in detail["handout"])

    draft = client.put(
        f"/api/packs/{pack_id}/draft",
        json={
            "answers": [
                {"question_id": detail["questions"][0]["id"], "selected_index": 0},
                {"question_id": detail["questions"][1]["id"], "selected_index": None},
            ]
        },
    )
    assert draft.status_code == 200
    assert draft.json()["answered_count"] == 1

    submit_body = {
        "idempotency_key": "submit-png-0001",
        "answers": [
            {"question_id": detail["questions"][0]["id"], "selected_index": 0},
            {"question_id": detail["questions"][1]["id"], "selected_index": 2},
            {"question_id": detail["questions"][2]["id"], "selected_index": None},
        ],
    }
    submitted = client.post(f"/api/packs/{pack_id}/submit", json=submit_body)
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()
    assert result["score"]["total"] == 3
    assert result["score"]["correct"] == 1
    assert result["score"]["wrong"] == 1
    assert result["score"]["unanswered"] == 1
    assert result["next_round_suggestion"]["available"] is True

    duplicate = client.post(f"/api/packs/{pack_id}/submit", json=submit_body)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["attempt_id"] == result["attempt_id"]
    assert duplicate.json()["score"] == result["score"]

    conflict = client.post(
        f"/api/packs/{pack_id}/submit",
        json={**submit_body, "idempotency_key": "submit-png-0002"},
    )
    assert conflict.status_code == 409

    reopened = client.get(f"/api/packs/{pack_id}").json()
    assert reopened["answer_revealed"] is True
    assert all("answer_index" in item for item in reopened["questions"])

    citation = reopened["questions"][0]["citations"][0]
    meta = client.get(f"/api/citations/{citation['id']}").json()
    assert meta["status"] == "ok"
    assert meta["text_alternative"]
    asset = client.get(f"/api/citations/{citation['id']}/asset")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "no-store"


def test_duplicate_generation_request_returns_same_job(client, topology_png):
    source = upload(client, topology_png)
    body = {
        "idempotency_key": "slice-png-idem-01",
        "sources": [{"source_id": source["id"], "page_start": None, "page_end": None}],
        "progress_text": "同一网段和跨网段发包容易混，想用这张图练一下。",
        "difficulty": "consolidate",
        "question_count": 3,
        "consent_ack": True,
    }
    first = client.post("/api/packs", json=body)
    second = client.post("/api/packs", json=body)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["duplicate"] is True