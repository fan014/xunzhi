from __future__ import annotations

from tests.stage4_helpers import generate_pack


def test_records_list_contains_pack_and_deletes_with_confirm(client, tmp_path):
    source, pack = generate_pack(client, tmp_path)
    listing = client.get("/api/packs?limit=50")
    assert listing.status_code == 200
    items = listing.json()["items"]
    row = next(item for item in items if item["id"] == pack["id"])
    assert row["question_count"] == 3
    assert row["source_count"] == 1
    assert row["attempt_status"] == "无作答"
    assert row["progress_excerpt"]

    refused = client.request("DELETE", f"/api/packs/{pack['id']}", json={"confirm": False})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "confirm_required"

    deleted = client.request("DELETE", f"/api/packs/{pack['id']}", json={"confirm": True})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/packs/{pack['id']}").status_code == 404

    sources = client.get("/api/sources").json()["items"]
    assert any(item["id"] == source["id"] for item in sources)


def test_deleting_one_pack_keeps_shared_source_for_other_pack(client, tmp_path):
    source, first = generate_pack(client, tmp_path)
    second_response = client.post(
        "/api/packs",
        json={
            "idempotency_key": "shared-second-01",
            "sources": [{"source_id": source["id"], "page_start": None, "page_end": None}],
            "progress_text": "同一网段与跨网段容易混",
            "difficulty": "consolidate",
            "question_count": 3,
            "consent_ack": True,
        },
    )
    assert second_response.status_code == 202
    from tests.stage4_helpers import wait_job

    second_job = wait_job(client, second_response.json()["job_id"])
    assert second_job["status"] == "succeeded"

    client.request("DELETE", f"/api/packs/{first['id']}", json={"confirm": True})
    still_there = client.get(f"/api/packs/{second_job['pack_id']}")
    assert still_there.status_code == 200
    assert still_there.json()["sources"][0]["status"] == "active"