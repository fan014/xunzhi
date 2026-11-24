from __future__ import annotations

from app.domain.knowledge import ensure_knowledge_point
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def _add_points(source_id: str, names: list[str]) -> None:
    with SessionLocal() as db:
        for name in names:
            ensure_knowledge_point(db, source_id, name)
        db.commit()


def _build_module_pack(client, tmp_path):
    source = helpers.upload(client, helpers.make_png(tmp_path / "topology.png", (61, 91, 121)))
    _add_points(
        source["id"],
        ["同网段判断", "跨网段转发", "默认网关", "MAC 地址转发", "设备角色对比"],
    )
    response = client.post(
        f"/api/sources/{source['id']}/next-plan",
        json={"goal": "完整学习这份资料", "learning_mode": "auto", "points": []},
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    version = plan["version"]
    assert len(version["modules"]) >= 2
    confirmed = client.post(
        f"/api/plans/{plan['plan']['id']}/versions/{version['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    created = client.post(
        "/api/packs",
        json={
            "idempotency_key": f"pages-{helpers.time.time_ns()}",
            "sources": [{"source_id": source["id"], "page_start": None, "page_end": None}],
            "progress_text": helpers.PROGRESS,
            "difficulty": "consolidate",
            "consent_ack": True,
            "plan_version_id": version["id"],
        },
    )
    assert created.status_code == 202, created.text
    job = helpers.wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    return client.get(f"/api/packs/{job['pack_id']}").json()


def test_module_pages_and_recap_are_persisted(client, tmp_path):
    detail = _build_module_pack(client, tmp_path)
    assert detail["modules"]
    page_types = set()
    for module in detail["modules"]:
        pages = module["pages"]
        assert pages, module["module_id"]
        assert module["recap"]["rules"], module["module_id"]
        page_ids = [page["page_id"] for page in pages]
        assert len(page_ids) == len(set(page_ids))
        for page in pages:
            assert page["title"]
            assert page["body"]
            assert page["page_type"] in {"text", "source_asset", "comparison", "step_sequence"}
            page_types.add(page["page_type"])
            visual = page["visual"]
            if page["page_type"] == "comparison":
                assert len(visual["cards"]) >= 2
            if page["page_type"] == "source_asset":
                assert visual["asset_url"]
                assert visual["citation_id"]
                asset = client.get(visual["asset_url"])
                assert asset.status_code == 200
                assert asset.headers["content-type"].startswith("image/")
                assert any(item["id"] == visual["citation_id"] for item in page["citations"])
        assert all(item["section_type"] not in {"module_page", "module_recap"} for item in module["handout"])
    assert "comparison" in page_types
    assert "source_asset" in page_types


def test_learning_last_restores_declared_page(client, tmp_path):
    detail = _build_module_pack(client, tmp_path)
    module = detail["modules"][0]
    pages = module["pages"]
    assert len(pages) >= 2
    target = pages[1]
    saved = client.put(
        f"/api/packs/{detail['id']}/position",
        json={"module_id": module["module_id"], "page_id": target["page_id"], "view": "modules"},
    )
    assert saved.status_code == 200, saved.text
    last = client.get("/api/learning/last").json()
    assert last["pack_id"] == detail["id"]
    assert last["module_id"] == module["module_id"]
    assert last["page_id"] == target["page_id"]
    assert last["view"] == "modules"
    fallback = client.put(
        f"/api/packs/{detail['id']}/position",
        json={"module_id": module["module_id"], "page_id": "missing-page", "view": "modules"},
    )
    assert fallback.status_code == 200
    assert client.get("/api/learning/last").json()["page_id"] == "missing-page"