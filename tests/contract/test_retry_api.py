from __future__ import annotations

from tests.stage4_helpers import create_pack, generate_pack, submit_wrong_once, wait_job


def test_review_focus_is_unavailable_before_submit(client, tmp_path):
    _, pack = generate_pack(client, tmp_path)
    payload = client.get(f"/api/packs/{pack['id']}/review-focus").json()
    assert payload["available"] is False
    assert "提交" in payload["reason"]


def test_review_focus_crud_and_retry_generation(client, tmp_path):
    _, pack = generate_pack(client, tmp_path)
    result = submit_wrong_once(client, pack)
    assert result["next_round_suggestion"]["available"] is True

    focus = client.get(f"/api/packs/{pack['id']}/review-focus").json()
    assert focus["available"] is True
    assert 1 <= len(focus["items"]) <= 5
    first = focus["items"][0]

    edited = client.put(
        f"/api/packs/{pack['id']}/review-focus/{first['id']}",
        json={"text": "改写的复习重点"},
    )
    assert edited.status_code == 200
    assert edited.json()["edited_text"] == "改写的复习重点"

    added = client.post(
        f"/api/packs/{pack['id']}/review-focus",
        json={"knowledge_point": "新增复习重点", "suggestion": ""},
    )
    assert added.status_code == 201

    if len(focus["items"]) > 1:
        removed = client.delete(
            f"/api/packs/{pack['id']}/review-focus/{focus['items'][1]['id']}"
        )
        assert removed.status_code == 200

    retry = create_pack(
        client,
        None,
        parent_pack_id=pack["id"],
        review_focus=["改写的复习重点", "新增复习重点"],
        key="retry-stage4-0001",
    )
    assert retry.status_code == 202, retry.text
    job = wait_job(client, retry.json()["job_id"])
    assert job["status"] == "succeeded", job
    child = client.get(f"/api/packs/{job['pack_id']}").json()
    assert child["parent_pack_id"] == pack["id"]

    parent = client.get(f"/api/packs/{pack['id']}").json()
    assert any(item["id"] == child["id"] for item in parent["children"])


def test_retry_requires_focus_and_submitted_parent(client, tmp_path):
    _, pack = generate_pack(client, tmp_path)
    no_focus = create_pack(
        client,
        None,
        parent_pack_id=pack["id"],
        review_focus=[],
        key="retry-no-focus-01",
    )
    assert no_focus.status_code in {409, 422}

    submit_wrong_once(client, pack)
    too_many = create_pack(
        client,
        None,
        parent_pack_id=pack["id"],
        review_focus=["a", "b", "c", "d", "e", "f"],
        key="retry-too-many-01",
    )
    assert too_many.status_code == 422