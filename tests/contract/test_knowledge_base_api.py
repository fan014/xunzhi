from __future__ import annotations

import time

from app.domain.knowledge import ensure_knowledge_point
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def _wait_kb_ready(client, kb_id, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = client.get(f"/api/knowledge-bases/{kb_id}").json()
        states = {item["status"] for item in last.get("items") or []}
        if states and states <= {"ready", "failed"}:
            return last
        time.sleep(0.2)
    raise AssertionError(f"knowledge base did not finish: {last}")


def test_knowledge_base_crud_scope_and_multi_source_learning(client, tmp_path):
    note_a = tmp_path / "course_a.txt"
    note_a.write_text("A 章同网段判断：交换机按 MAC 地址转发；A 章交换机转发：同网段不经过路由器。", encoding="utf-8")
    note_b = tmp_path / "course_b.txt"
    note_b.write_text("B 章跨网段转发：跨网段需要默认网关；B 章默认网关：路由器连接不同网段。", encoding="utf-8")
    source_a = helpers.upload(client, note_a)
    source_b = helpers.upload(client, note_b)
    with SessionLocal() as db:
        for name in ("A 章同网段判断", "A 章交换机转发"):
            ensure_knowledge_point(db, source_a["id"], name)
        for name in ("B 章跨网段转发", "B 章默认网关"):
            ensure_knowledge_point(db, source_b["id"], name)
        db.commit()

    created = client.post(
        "/api/knowledge-bases", json={"name": "网络基础课", "description": "两个章节"}
    )
    assert created.status_code == 201, created.text
    kb_a = created.json()
    added_a = client.post(
        f"/api/knowledge-bases/{kb_a['id']}/items", json={"source_id": source_a["id"]}
    )
    assert added_a.status_code == 201, added_a.text
    added_b = client.post(
        f"/api/knowledge-bases/{kb_a['id']}/items", json={"source_id": source_b["id"]}
    )
    assert added_b.status_code == 201, added_b.text
    detail = _wait_kb_ready(client, kb_a["id"])
    assert detail["item_count"] == 2 and detail["ready_count"] == 2

    kb_b = client.post("/api/knowledge-bases", json={"name": "另一门课"}).json()
    client.post(
        f"/api/knowledge-bases/{kb_b['id']}/items", json={"source_id": source_b["id"]}
    )

    # 修改显示名与笔记
    item_id = detail["items"][0]["id"]
    updated = client.patch(
        f"/api/knowledge-bases/{kb_a['id']}/items/{item_id}",
        json={"display_name": "第一章资料", "note": "重点看交换机"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["items"][0]["display_name"] == "第一章资料"

    started = client.post(
        f"/api/sources/{source_a['id']}/diagnosis/sessions",
        json={
            "idempotency_key": f"kb-dx-{time.time_ns()}",
            "goal": "整库学习网络基础",
            "learning_mode": "auto",
            "knowledge_base_id": kb_a["id"],
            "scope_mode": "all",
        },
    )
    assert started.status_code == 201, started.text
    session = started.json()["session"]
    for _ in range(7):
        question = session.get("current_question")
        if not question:
            break
        answered = client.post(
            f"/api/diagnosis/sessions/{session['id']}/answers",
            json={
                "idempotency_key": f"kb-answer-{time.time_ns()}",
                "question_id": question["id"],
                "selected_index": 0,
                "status": "answered",
            },
        )
        assert answered.status_code == 200, answered.text
        session = answered.json()

    preview = client.post(
        f"/api/diagnosis/sessions/{session['id']}/finish",
        json={"goal": "整库学习网络基础", "learning_mode": "auto"},
    )
    assert preview.status_code == 200, preview.text
    version = preview.json()["version"]
    assert version["modules"]
    module_sources = {
        source_id
        for module in version["modules"]
        for source_id in module.get("source_ids") or []
    }
    assert {source_a["id"], source_b["id"]} <= module_sources
    confirmed = client.post(
        f"/api/diagnosis/sessions/{session['id']}/confirm",
        json={"idempotency_key": f"kb-confirm-{time.time_ns()}"},
    )
    assert confirmed.status_code == 200, confirmed.text
    job = helpers.wait_job(client, confirmed.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    detail_pack = client.get(f"/api/packs/{job['pack_id']}").json()
    assert len(detail_pack["questions"]) == version["question_count"]
    assert len(detail_pack["modules"]) == len(version["modules"])
    assert detail_pack["modules"]

    # KB 隔离：另一库只有 source_b
    assert client.get(f"/api/knowledge-bases/{kb_b['id']}").json()["item_count"] == 1
    removed = client.delete(f"/api/knowledge-bases/{kb_a['id']}/items/{item_id}")
    assert removed.status_code == 200
    sources = client.get("/api/sources").json()["items"]
    assert any(item["id"] == source_a["id"] for item in sources)
    # 删除库只解除关联
    deleted = client.request(
        "DELETE", f"/api/knowledge-bases/{kb_b['id']}", json={"confirm": True}
    )
    assert deleted.status_code == 200
    sources = client.get("/api/sources").json()["items"]
    assert any(item["id"] == source_b["id"] for item in sources)