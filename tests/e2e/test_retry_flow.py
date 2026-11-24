from __future__ import annotations

from tests.stage4_helpers import create_pack, generate_pack, submit_wrong_once, wait_job


def test_retry_chain_is_bidirectionally_traceable(client, tmp_path):
    _, parent = generate_pack(client, tmp_path)
    submit_wrong_once(client, parent)
    focus = client.get(f"/api/packs/{parent['id']}/review-focus").json()
    texts = [item["edited_text"] or item["knowledge_point"] for item in focus["items"]]

    response = create_pack(
        client,
        None,
        parent_pack_id=parent["id"],
        review_focus=texts[:5],
        key="retry-chain-0001",
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "succeeded", job
    child = client.get(f"/api/packs/{job['pack_id']}").json()
    assert child["parent_pack_id"] == parent["id"]

    parent_again = client.get(f"/api/packs/{parent['id']}").json()
    assert any(item["id"] == child["id"] for item in parent_again["children"])


def test_all_correct_shows_completion_without_retry_entry(client, tmp_path):
    _, pack = generate_pack(client, tmp_path)
    answers = [
        {"question_id": pack["questions"][0]["id"], "selected_index": 1},
        {"question_id": pack["questions"][1]["id"], "selected_index": 2},
        {"question_id": pack["questions"][2]["id"], "selected_index": 0},
    ]
    result = client.post(
        f"/api/packs/{pack['id']}/submit",
        json={"idempotency_key": "all-correct-0001", "answers": answers},
    ).json()
    assert result["score"]["correct"] == 3
    assert result["score"]["wrong"] == 0
    assert result["next_round_suggestion"]["available"] is False
    focus = client.get(f"/api/packs/{pack['id']}/review-focus").json()
    assert focus["available"] is False


def test_parent_deleted_does_not_block_child_reading(client, tmp_path):
    _, parent = generate_pack(client, tmp_path)
    submit_wrong_once(client, parent)
    focus = client.get(f"/api/packs/{parent['id']}/review-focus").json()
    texts = [item["knowledge_point"] for item in focus["items"]][:5]
    created = create_pack(
        client, None, parent_pack_id=parent["id"], review_focus=texts, key="retry-parent-delete-01"
    )
    job = wait_job(client, created.json()["job_id"])
    child = client.get(f"/api/packs/{job['pack_id']}").json()

    client.request("DELETE", f"/api/packs/{parent['id']}", json={"confirm": True})
    reopened = client.get(f"/api/packs/{child['id']}")
    assert reopened.status_code == 200
    assert reopened.json()["parent_status"] == "deleted"
    assert reopened.json()["questions"]