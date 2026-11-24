from __future__ import annotations

import json
import time
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.domain.generation import assemble_for_sources
from app.domain.knowledge import ensure_knowledge_point
from app.domain.models import AppSetting, Evidence, ImageAsset, SourceMaterial, SourcePage
from app.infra.db import SessionLocal
from app.infra.source_service import ensure_pdf_region_evidence
from tests import stage4_helpers as helpers

LENS_PDF = Path(__file__).resolve().parent.parent.parent / "samples" / "lens_two_conditions.pdf"


def _unique_lens_copy(tmp_path: Path) -> Path:
    assert LENS_PDF.exists(), "请先运行 python scripts/make_lens_acceptance_pdf.py"
    doc = pymupdf.open(str(LENS_PDF))
    doc[0].insert_text((20, 575), f"copy-{time.time_ns()}", fontsize=8)
    target = tmp_path / f"lens_kb_{time.time_ns()}.pdf"
    doc.save(str(target))
    doc.close()
    return target


def _wait_kb_ready(client, kb_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = client.get(f"/api/knowledge-bases/{kb_id}").json()
        states = {item["status"] for item in last.get("items") or []}
        if states and states <= {"ready", "failed"}:
            return last
        time.sleep(0.2)
    raise AssertionError(f"knowledge base did not finish: {last}")


def _upload_lens_to_kb(client, tmp_path: Path) -> tuple[str, str]:
    pdf = _unique_lens_copy(tmp_path)
    return _upload_pdf_to_kb(client, tmp_path, pdf, "透镜库")


def _upload_pdf_to_kb(client, tmp_path: Path, pdf: Path, name: str) -> tuple[str, str]:
    created = client.post("/api/knowledge-bases", json={"name": f"{name}-{time.time_ns()}"})
    assert created.status_code == 201, created.text
    kb_id = created.json()["id"]
    with pdf.open("rb") as fh:
        uploaded = client.post(
            f"/api/knowledge-bases/{kb_id}/items/upload",
            files={"files": (pdf.name, fh, "application/pdf")},
        )
    assert uploaded.status_code == 202, uploaded.text
    assert not uploaded.json().get("upload_errors"), uploaded.json()
    detail = _wait_kb_ready(client, kb_id)
    item = next(item for item in detail["items"] if item["status"] == "ready")
    assert item["source_id"]
    return kb_id, item["source_id"]


def _multi_page_image_pdf(tmp_path: Path, pages: int = 12) -> Path:
    figure = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(figure)
    draw.rectangle([40, 40, 560, 360], outline="#315c93", width=4)
    draw.text((60, 180), "figure on every text page", fill="#1d2430")
    image_path = tmp_path / "page_figure.png"
    figure.save(image_path)

    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=842, height=595)
        page.insert_text((40, 40), f"Multi-page text page {index + 1}", fontsize=14)
        page.insert_text((40, 62), "This page has extractable text and one embedded image.", fontsize=10)
        page.insert_image(pymupdf.Rect(60, 110, 760, 560), filename=str(image_path))
    target = tmp_path / f"multi_image_{time.time_ns()}.pdf"
    doc.save(str(target))
    doc.close()
    return target


def _region_rows(db, source_id: str) -> list[Evidence]:
    return db.execute(
        select(Evidence).where(
            Evidence.source_id == source_id,
            Evidence.kind == "image_region",
            Evidence.status == "active",
        )
    ).scalars().all()


def test_knowledge_base_lazy_pdf_registers_regions_without_rendering_pages(client, tmp_path):
    _kb_id, source_id = _upload_lens_to_kb(client, tmp_path)
    with SessionLocal() as db:
        regions = _region_rows(db, source_id)
        assert len(regions) >= 2
        pages = sorted({row.page_no for row in regions})
        assert pages == [1, 2]
        for row in regions:
            locator = json.loads(row.locator_json)
            assert locator.get("boxes")
            assert not locator.get("asset_id")
            assert locator.get("deferred") is True
        source_pages = db.execute(
            select(SourcePage).where(SourcePage.source_id == source_id)
        ).scalars().all()
        assert source_pages
        assert all(page.page_image_asset_id is None for page in source_pages)
        assets = db.execute(
            select(ImageAsset).where(ImageAsset.source_id == source_id)
        ).scalars().all()
        assert assets == []


def test_knowledge_base_lazy_pdf_renders_only_selected_pages_and_reuses(client, tmp_path):
    _kb_id, source_id = _upload_lens_to_kb(client, tmp_path)
    with SessionLocal() as db:
        assembly = assemble_for_sources(
            db,
            [{"source_id": source_id, "page_start": 1, "page_end": 2}],
            load_images=True,
            text_limit=20,
            image_limit=2,
            balanced=True,
        )
        assert {"I1", "I2"}.issubset(set(assembly.ref_map.targets))
        descriptions = {item.ref: item.description for item in assembly.images}
        assert "第 1 页" in descriptions["I1"]
        assert "第 2 页" in descriptions["I2"]
        assets = db.execute(
            select(ImageAsset).where(ImageAsset.source_id == source_id)
        ).scalars().all()
        assert len(assets) == 2
        asset_ids = {asset.id for asset in assets}
        pages = db.execute(
            select(SourcePage).where(SourcePage.source_id == source_id)
        ).scalars().all()
        assert all(page.page_image_asset_id in asset_ids for page in pages)
        db.commit()

    with SessionLocal() as db:
        before = db.execute(
            select(ImageAsset.id).where(ImageAsset.source_id == source_id)
        ).scalars().all()
        assembly = assemble_for_sources(
            db,
            [{"source_id": source_id, "page_start": 1, "page_end": 2}],
            load_images=True,
            text_limit=20,
            image_limit=2,
            balanced=True,
        )
        assert {"I1", "I2"}.issubset(set(assembly.ref_map.targets))
        after = db.execute(
            select(ImageAsset.id).where(ImageAsset.source_id == source_id)
        ).scalars().all()
        assert sorted(before) == sorted(after)
        db.commit()


def test_old_source_without_regions_is_backfilled_once(client, tmp_path):
    pdf = _unique_lens_copy(tmp_path)
    source = helpers.upload(client, pdf)
    source_id = source["id"]

    with SessionLocal() as db:
        for row in _region_rows(db, source_id):
            db.delete(row)
        marker = db.get(AppSetting, f"pdf.region_index.{source_id}")
        if marker is not None:
            db.delete(marker)
        db.commit()

    with SessionLocal() as db:
        material = db.get(SourceMaterial, source_id)
        added = ensure_pdf_region_evidence(db, material)
        db.commit()
        assert added == 2
        first = sorted(row.id for row in _region_rows(db, source_id))

    with SessionLocal() as db:
        material = db.get(SourceMaterial, source_id)
        added = ensure_pdf_region_evidence(db, material)
        db.commit()
        assert added == 0
        second = sorted(row.id for row in _region_rows(db, source_id))
        assert first == second
        assert db.get(SourceMaterial, source_id) is not None

def test_knowledge_base_pack_contains_visible_source_asset(client, tmp_path):
    _kb_id, source_id = _upload_lens_to_kb(client, tmp_path)
    with SessionLocal() as db:
        for name in ("同网段判断", "跨网段转发", "默认网关", "MAC 地址转发", "设备角色对比"):
            ensure_knowledge_point(db, source_id, name)
        db.commit()

    plan_response = client.post(
        f"/api/sources/{source_id}/next-plan",
        json={"goal": "对照两页透镜原图学习", "learning_mode": "auto", "points": []},
    )
    assert plan_response.status_code == 201, plan_response.text
    plan = plan_response.json()
    version = plan["version"]
    confirmed = client.post(
        f"/api/plans/{plan['plan']['id']}/versions/{version['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    created = client.post(
        "/api/packs",
        json={
            "idempotency_key": f"kb-lens-{time.time_ns()}",
            "sources": [{"source_id": source_id, "page_start": None, "page_end": None}],
            "progress_text": "我想比较这两页透镜成像条件。",
            "difficulty": "consolidate",
            "consent_ack": True,
            "plan_version_id": version["id"],
        },
    )
    assert created.status_code == 202, created.text
    job = helpers.wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    detail = client.get(f"/api/packs/{job['pack_id']}").json()
    pages = [
        page
        for module in detail["modules"]
        for page in module["pages"]
        if page.get("page_type") == "source_asset"
    ]
    assert pages, "知识库入口生成的学习包缺少资料原图页"
    visual = pages[0]["visual"]
    assert visual.get("source_ref")
    assert visual.get("citation_id")
    assert visual.get("asset_url")
    asset = client.get(visual["asset_url"])
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("image/")

def test_multi_page_text_pdf_registers_regions_and_renders_only_selected_pages(
    client, tmp_path
):
    pdf = _multi_page_image_pdf(tmp_path, pages=12)
    _kb_id, source_id = _upload_pdf_to_kb(client, tmp_path, pdf, "多页图文库")

    with SessionLocal() as db:
        regions = _region_rows(db, source_id)
        assert len(regions) == 12
        assert sorted({row.page_no for row in regions}) == list(range(1, 13))
        assets = db.execute(
            select(ImageAsset).where(ImageAsset.source_id == source_id)
        ).scalars().all()
        assert assets == []
        pages = db.execute(
            select(SourcePage).where(SourcePage.source_id == source_id)
        ).scalars().all()
        assert all(page.page_image_asset_id is None for page in pages)

    with SessionLocal() as db:
        assembly = assemble_for_sources(
            db,
            [{"source_id": source_id, "page_start": None, "page_end": None}],
            load_images=True,
            text_limit=40,
            image_limit=3,
            balanced=True,
        )
        assert len(assembly.images) == 3
        assets = db.execute(
            select(ImageAsset).where(ImageAsset.source_id == source_id)
        ).scalars().all()
        assert len(assets) == 3
        pages = db.execute(
            select(SourcePage).where(SourcePage.source_id == source_id)
        ).scalars().all()
        rendered_pages = [page.page_no for page in pages if page.page_image_asset_id]
        assert len(rendered_pages) == 3
        assert len(set(rendered_pages)) == 3
        db.commit()