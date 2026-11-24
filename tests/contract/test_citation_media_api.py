from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.domain.models import (
    Citation,
    Evidence,
    LearningPack,
    PackSource,
    SourcePage,
    now_iso,
)
from app.infra.db import SessionLocal
from tests.stage4_helpers import upload

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def find_evidence(db, source_id: str, kind: str, page_no):
    return db.execute(
        select(Evidence)
        .where(
            Evidence.source_id == source_id,
            Evidence.kind == kind,
            Evidence.page_no == page_no if page_no is not None else Evidence.page_no.is_(None),
        )
        .order_by(Evidence.ordinal)
    ).scalars().first()


def seed_pack(db, source, source_kind: str, page_no: int | None):
    pack_id = "pack_citation_" + uuid.uuid4().hex[:8]
    pack = LearningPack(
        id=pack_id,
        display_title="引用定位测试",
        source_kind="real",
        difficulty="consolidate",
        question_count=3,
        job_id="job_citation_" + uuid.uuid4().hex[:8],
        handout_word_count=0,
        model_name="deepseek-flash",
        prompt_version="prompt_v1",
        created_at=now_iso(),
    )
    db.add(pack)
    db.add(
        PackSource(
            pack_id=pack_id,
            source_id=source.id,
            page_start=page_no,
            page_end=page_no,
            page_count_used=1 if page_no else None,
            source_name_snapshot=source.display_name,
            source_sha256_snapshot=source.sha256,
            source_kind_snapshot=source_kind,
        )
    )
    db.flush()
    return pack_id


def seed_citation(db, pack_id: str, source, evidence, locator: dict, quote: str, claim: str):
    citation = Citation(
        id="cit_citation_" + uuid.uuid4().hex[:8],
        pack_id=pack_id,
        owner_type="section",
        owner_id="sec_citation",
        evidence_id=evidence.id,
        source_id=source.id,
        page_no=evidence.page_no,
        locator_json=json.dumps(locator, ensure_ascii=False),
        quote_snapshot=quote,
        claim_text=claim,
        local_ref="T1",
        created_at=now_iso(),
    )
    db.add(citation)
    db.commit()
    return citation.id


@pytest.fixture
def cleanup_packs(client):
    packs = []
    yield packs
    for pack_id in packs:
        client.request("DELETE", f"/api/packs/{pack_id}", json={"confirm": True})


def test_pdf_text_span_resolves_page_png_and_rects(client, cleanup_packs):
    source_dict = upload(client, FIXTURES / "pdf" / "sample_3pages.pdf")
    with SessionLocal() as db:
        from app.domain.models import SourceMaterial

        source = db.get(SourceMaterial, source_dict["id"])
        page = db.get(SourcePage, (source.id, 2))
        assert page is not None and page.page_image_asset_id
        evidence = find_evidence(db, source.id, "text_span", 2)
        assert evidence is not None and json.loads(evidence.locator_json).get("rects")
        pack_id = seed_pack(db, source, "pdf", 2)
        cleanup_packs.append(pack_id)
        citation_id = seed_citation(
            db,
            pack_id,
            source,
            evidence,
            json.loads(evidence.locator_json),
            "交换机按目的 MAC 地址转发。",
            "交换机按 MAC 转发",
        )

    payload = client.get(f"/api/citations/{citation_id}").json()
    assert payload["status"] == "ok"
    assert payload["page_no"] == 2
    assert payload["asset_kind"] == "page_image"
    assert payload["asset_url"]
    assert payload["boxes"]
    assert payload["scanned"] is False
    asset = client.get(payload["asset_url"])
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("image/png")


def test_txt_text_span_has_no_image_asset(client, cleanup_packs):
    source_dict = upload(client, FIXTURES / "text" / "note.txt")
    with SessionLocal() as db:
        from app.domain.models import SourceMaterial

        source = db.get(SourceMaterial, source_dict["id"])
        evidence = find_evidence(db, source.id, "text_span", None)
        assert evidence is not None
        pack_id = seed_pack(db, source, "text", None)
        cleanup_packs.append(pack_id)
        citation_id = seed_citation(
            db,
            pack_id,
            source,
            evidence,
            json.loads(evidence.locator_json),
            "交换机转发基础",
            "交换机转发基础",
        )

    payload = client.get(f"/api/citations/{citation_id}").json()
    assert payload["status"] == "ok"
    assert payload["asset_url"] is None
    assert payload["text_only"] is True
    assert payload["asset_expected"] is False
    assert payload["quote"]


def test_scanned_page_marks_citation_and_uses_page_image(client, cleanup_packs):
    source_dict = upload(client, FIXTURES / "pdf" / "sample_3pages.pdf")
    with SessionLocal() as db:
        from app.domain.models import SourceMaterial

        source = db.get(SourceMaterial, source_dict["id"])
        page = db.get(SourcePage, (source.id, 3))
        assert page is not None and page.page_image_asset_id and page.is_scanned
        evidence = find_evidence(db, source.id, "page_image", 3)
        assert evidence is not None
        pack_id = seed_pack(db, source, "pdf", 3)
        cleanup_packs.append(pack_id)
        citation_id = seed_citation(
            db,
            pack_id,
            source,
            evidence,
            json.loads(evidence.locator_json),
            "扫描页整页",
            "扫描页",
        )

    payload = client.get(f"/api/citations/{citation_id}").json()
    assert payload["status"] == "ok"
    assert payload["scanned"] is True
    assert payload["asset_url"]
    asset = client.get(payload["asset_url"])
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("image/png")


def test_image_box_and_description_are_exposed(client, cleanup_packs):
    source_dict = upload(client, FIXTURES / "pdf" / "sample_3pages.pdf")
    with SessionLocal() as db:
        from app.domain.models import SourceMaterial

        source = db.get(SourceMaterial, source_dict["id"])
        evidence = find_evidence(db, source.id, "image_region", 3)
        assert evidence is not None
        base_locator = json.loads(evidence.locator_json)
        parent = base_locator.get("boxes") or [base_locator.get("rect", [0.0, 0.0, 1.0, 1.0])]
        child = [0.2, 0.2, 0.5, 0.5]
        locator = {**base_locator, "boxes": parent, "box_2d": child, "image_description": "图中可见 PC-A 与 SW1 的连线"}
        pack_id = seed_pack(db, source, "pdf", 3)
        cleanup_packs.append(pack_id)
        citation_id = seed_citation(db, pack_id, source, evidence, locator, "图像区域", "引用说明")

    payload = client.get(f"/api/citations/{citation_id}").json()
    assert payload["status"] == "ok"
    assert payload["boxes"] == [child]
    assert payload["text_alternative"] == "图中可见 PC-A 与 SW1 的连线"
    assert payload["text_alternative_source"] == "image_description"


def test_invalid_rects_return_locator_invalid_and_keep_summary(client, cleanup_packs):
    source_dict = upload(client, FIXTURES / "pdf" / "sample_3pages.pdf")
    with SessionLocal() as db:
        from app.domain.models import SourceMaterial

        source = db.get(SourceMaterial, source_dict["id"])
        evidence = find_evidence(db, source.id, "text_span", 2)
        assert evidence is not None
        pack_id = seed_pack(db, source, "pdf", 2)
        cleanup_packs.append(pack_id)
        citation_id = seed_citation(
            db,
            pack_id,
            source,
            evidence,
            {"rects": [[0.8, 0.8, 0.2, 0.2]]},
            "应保留的文字摘要",
            "摘要",
        )

    payload = client.get(f"/api/citations/{citation_id}").json()
    assert payload["status"] == "locator_invalid"
    assert payload["asset_url"] is None
    assert payload["quote"] == "应保留的文字摘要"