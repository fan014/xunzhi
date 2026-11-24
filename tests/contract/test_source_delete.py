from __future__ import annotations

from tests.stage4_helpers import generate_pack


def first_question_citation(pack: dict) -> dict:
    for section in pack.get("handout", []):
        if section.get("citations"):
            return section["citations"][0]
    for item in pack.get("questions", []):
        if item.get("citations"):
            return item["citations"][0]
    raise AssertionError("pack has no citation")


def test_source_delete_requires_confirm_and_preserves_history(client, tmp_path):
    source, pack = generate_pack(client, tmp_path)
    impact = client.get(f"/api/sources/{source['id']}/impact")
    assert impact.status_code == 200
    assert impact.json()["referenced_pack_count"] >= 1

    refused = client.request("DELETE", f"/api/sources/{source['id']}", json={"confirm": False})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "confirm_required"
    # 取消后数据完整
    assert client.get(f"/api/packs/{pack['id']}").status_code == 200

    deleted = client.request("DELETE", f"/api/sources/{source['id']}", json={"confirm": True})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    detail = client.get(f"/api/packs/{pack['id']}")
    assert detail.status_code == 200
    assert detail.json()["sources"][0]["status"] == "deleted"
    assert detail.json()["questions"]

    # 资料已删，引用仍返回摘要；图像地址 410
    citation = first_question_citation(detail.json())
    meta = client.get(f"/api/citations/{citation['id']}")
    assert meta.status_code == 200
    assert meta.json()["status"] == "source_deleted"
    asset = client.get(f"/api/citations/{citation['id']}/asset")
    assert asset.status_code == 410
    assert asset.json()["error"]["code"] == "source_deleted"

    listing = client.get("/api/sources").json()["items"]
    assert all(item["id"] != source["id"] for item in listing)


def test_deleted_source_blocks_retry_without_silent_replacement(client, tmp_path):
    source, pack = generate_pack(client, tmp_path)
    client.post(
        f"/api/packs/{pack['id']}/submit",
        json={
            "idempotency_key": "delete-retry-submit-01",
            "answers": [{"question_id": q["id"], "selected_index": 0} for q in pack["questions"]],
        },
    )
    client.request("DELETE", f"/api/sources/{source['id']}", json={"confirm": True})
    retry = client.post(
        "/api/packs",
        json={
            "idempotency_key": "delete-retry-parent-01",
            "progress_text": "针对错题再练",
            "difficulty": "consolidate",
            "question_count": 3,
            "consent_ack": True,
            "parent_pack_id": pack["id"],
            "review_focus": ["同一网段内交换机转发的判断依据"],
        },
    )
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "source_deleted"
    assert retry.json()["error"]["actions"]