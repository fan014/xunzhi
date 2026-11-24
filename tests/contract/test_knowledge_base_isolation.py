from __future__ import annotations

import time
from pathlib import Path

from app.domain import knowledge_base as kb_service
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def _wait_item(client, kb_id: str, item_id: str, *, status: str, version: int | None = None, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        detail = client.get(f"/api/knowledge-bases/{kb_id}").json()
        item = next((row for row in detail.get("items") or [] if row["id"] == item_id), None)
        if item is not None:
            last = item
            if item["status"] == status and (version is None or item["version_no"] == version):
                return item
        time.sleep(0.2)
    raise AssertionError(f"item did not reach {status}: {last}")


def test_rename_replace_and_old_pack_stay_isolated(client, tmp_path):
    original = helpers.make_png(tmp_path / "original.png")
    source = helpers.upload(client, original)
    original_name = source["display_name"]

    kb_a = client.post("/api/knowledge-bases", json={"name": "A 库"}).json()
    kb_b = client.post("/api/knowledge-bases", json={"name": "B 库"}).json()
    for kb in (kb_a, kb_b):
        added = client.post(
            f"/api/knowledge-bases/{kb['id']}/items", json={"source_id": source["id"]}
        )
        assert added.status_code == 201, added.text

    detail_a = client.get(f"/api/knowledge-bases/{kb_a['id']}").json()
    item_id = detail_a["items"][0]["id"]

    renamed = client.patch(
        f"/api/knowledge-bases/{kb_a['id']}/items/{item_id}",
        json={"display_name": "A 库里的第一章"},
    )
    assert renamed.status_code == 200, renamed.text

    detail_b = client.get(f"/api/knowledge-bases/{kb_b['id']}").json()
    assert detail_b["items"][0]["display_name"] == original_name
    sources = client.get("/api/sources").json()["items"]
    assert next(row for row in sources if row["id"] == source["id"])["display_name"] == original_name

    created = helpers.create_pack(client, source["id"], count=3)
    assert created.status_code == 202, created.text
    job = helpers.wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    pack_id = job["pack_id"]
    pack_before = client.get(f"/api/packs/{pack_id}")
    assert pack_before.status_code == 200

    broken = Path(__file__).resolve().parent.parent / "fixtures" / "broken" / "fake.pdf"
    with open(broken, "rb") as fh:
        replaced = client.post(
            f"/api/knowledge-bases/{kb_a['id']}/items/{item_id}/replace",
            files={"file": ("broken.pdf", fh, "application/pdf")},
        )
    assert replaced.status_code == 202, replaced.text

    failed_item = _wait_item(client, kb_a["id"], item_id, status="failed")
    assert failed_item["usable"] is True
    assert failed_item["source_id"] == source["id"]
    assert failed_item["error_message"]
    with SessionLocal() as db:
        specs = kb_service.scope_specs(db, kb_a["id"])
    assert [spec["source_id"] for spec in specs] == [source["id"]]

    replacement = tmp_path / "replacement.txt"
    replacement.write_text("第一章 跨网段转发：跨网段需要默认网关。", encoding="utf-8")
    with open(replacement, "rb") as fh:
        reuploaded = client.post(
            f"/api/knowledge-bases/{kb_a['id']}/items/{item_id}/replace",
            files={"file": ("replacement.txt", fh, "text/plain")},
        )
    assert reuploaded.status_code == 202, reuploaded.text

    ready_item = _wait_item(client, kb_a["id"], item_id, status="ready", version=2)
    assert ready_item["usable"] is True
    assert ready_item["source_id"] != source["id"]
    with SessionLocal() as db:
        specs = kb_service.scope_specs(db, kb_a["id"])
    assert [spec["source_id"] for spec in specs] == [ready_item["source_id"]]

    pack_after = client.get(f"/api/packs/{pack_id}")
    assert pack_after.status_code == 200
    detail_pack = pack_after.json()
    citations = [
        citation
        for section in detail_pack.get("handout") or []
        for citation in section.get("citations") or []
    ]
    assert citations
    assert all(citation["source_name"] == original_name for citation in citations)
    assert all(citation["source_status"] == "active" for citation in citations)