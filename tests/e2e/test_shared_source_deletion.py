from __future__ import annotations

from tests.stage4_helpers import create_pack, generate_pack, wait_job


def submit_all_first_option(client, pack: dict) -> dict:
    response = client.post(
        f"/api/packs/{pack['id']}/submit",
        json={
            "idempotency_key": f"shared-submit-{pack['id'][-8:]}",
            "answers": [{"question_id": item["id"], "selected_index": 0} for item in pack["questions"]],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_two_packs_share_source_then_delete_pack_and_source(client, tmp_path):
    source, first = generate_pack(client, tmp_path)
    second_response = create_pack(client, source["id"], count=3, key="shared-delete-02")
    assert second_response.status_code == 202
    second_job = wait_job(client, second_response.json()["job_id"])
    assert second_job["status"] == "succeeded"
    second = client.get(f"/api/packs/{second_job['pack_id']}").json()

    # 先交卷再读取题目引用，交卷前题目引用保持隐藏。
    result = submit_all_first_option(client, second)
    citation = next(
        item["citations"][0] for item in result["questions"] if item.get("citations")
    )
    assert client.get(f"/api/citations/{citation['id']}/asset").status_code == 200

    # 删除第一包：删包成功、第一包不可访问、第二包与资料仍可用。
    deleted = client.request("DELETE", f"/api/packs/{first['id']}", json={"confirm": True})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert client.get(f"/api/packs/{first['id']}").status_code == 404
    still_open = client.get(f"/api/packs/{second['id']}")
    assert still_open.status_code == 200
    assert still_open.json()["sources"][0]["status"] == "active"
    assert still_open.json()["result"]["score"] == result["score"]
    sources = client.get("/api/sources").json()["items"]
    assert any(item["id"] == source["id"] for item in sources)

    # 再删除资料：历史成绩与解析保留，引用标记已删，旧预览地址 410。
    removed = client.request("DELETE", f"/api/sources/{source['id']}", json={"confirm": True})
    assert removed.status_code == 200
    history_response = client.get(f"/api/packs/{second['id']}")
    assert history_response.status_code == 200
    history = history_response.json()
    assert history["sources"][0]["status"] == "deleted"
    assert history["result"]["score"] == result["score"]
    assert history["result"]["questions"]
    assert all(item["explanation"] for item in history["result"]["questions"])
    assert history["questions"]

    meta = client.get(f"/api/citations/{citation['id']}")
    assert meta.status_code == 200
    assert meta.json()["status"] == "source_deleted"
    assert meta.json()["quote"]
    assert client.get(f"/api/citations/{citation['id']}/asset").status_code == 410
    assert client.get(f"/api/citations/{citation['id']}/asset?old=1").status_code == 410