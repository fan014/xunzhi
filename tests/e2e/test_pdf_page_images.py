from __future__ import annotations

import base64
import io
import json

import pymupdf
import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select

from app.domain import generation
from app.domain.models import (
    Citation,
    Evidence,
    GenerationEvidence,
    ImageAsset,
    SourcePage,
)
from app.infra.db import SessionLocal
from app.infra.model_client import DeepSeekClient, ModelResult
from tests.stage4_helpers import upload, wait_job


def make_pdf(path, seed: int, scanned_page_two: bool) -> None:
    doc = pymupdf.open()
    for page_no in (1, 2, 3):
        page = doc.new_page(width=595, height=842)
        if page_no == 2 and scanned_page_two:
            image = Image.new("RGB", (1240, 1754), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle([120, 220, 520, 360], outline="#1f6b4a", width=8)
            draw.rectangle([700, 700, 1080, 860], outline="#2f6fa8", width=8)
            draw.line([520, 290, 700, 780], fill="#5b6b62", width=8)
            draw.text((160, 270), "PC-A", fill="#17211c")
            draw.text((760, 760), "SW1", fill="#17211c")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            page.insert_image(page.rect, stream=buffer.getvalue())
            continue
        page.insert_text((72, 100), f"PDF {seed} - page {page_no}", fontsize=18)
        page.insert_text((72, 140), "A switch forwards frames by destination MAC address.", fontsize=11)
        if page_no == 2:
            page.draw_rect(pymupdf.Rect(90, 200, 240, 300), color=(0.12, 0.42, 0.29), width=2)
            page.draw_line(pymupdf.Point(240, 250), pymupdf.Point(420, 420), color=(0.35, 0.4, 0.38), width=2)
            page.insert_text((110, 245), "PC-A", fontsize=11)
            page.insert_text((430, 430), "SW1", fontsize=11)
    doc.save(path)
    doc.close()


class RecordingModel:
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        page_refs = [image.ref for image in request.images if image.ref.startswith("P")]
        assert len(page_refs) == 2, page_refs
        first, second = page_refs
        output = {
            "progress_reading": {
                "learned": ["交换机"],
                "unlearned": [],
                "weak_points": ["设备与拓扑"],
                "intent": ["读图练习"],
                "insufficient": False,
            },
            "handout": {
                "scope": {"covered": ["图中设备与直接连线"], "not_covered": ["路由协议细节"]},
                "key_points": [
                    {"heading": "要点一", "body": "第一条", "role": "weak", "citations": [{"ref": first}]},
                    {"heading": "要点二", "body": "第二条", "role": "weak", "citations": [{"ref": second}]},
                    {"heading": "要点三", "body": "第三条", "role": "neutral", "citations": [{"ref": first}]},
                ],
                "pitfalls": [
                    {"heading": "易错一", "body": "错误一", "role": "weak", "citations": [{"ref": first}]},
                    {"heading": "易错二", "body": "错误二", "role": "weak", "citations": [{"ref": second}]},
                ],
                "visual": [
                    {"heading": "看图", "body": "两条页图", "role": "weak", "citations": [{"ref": second}]}
                ],
            },
            "questions": [
                {
                    "ordinal": 1,
                    "stem": "图中哪个设备连接了左侧主机？",
                    "options": ["A 选项", "B 选项", "C 选项", "D 选项"],
                    "answer_index": 0,
                    "explanation": "解析一",
                    "knowledge_point": "设备识别",
                    "difficulty_tag": "consolidate",
                    "is_image_based": True,
                    "citations": [{"ref": first}],
                },
                {
                    "ordinal": 2,
                    "stem": "图中另一份资料的页图属于哪种页面？",
                    "options": ["A 选项", "B 选项", "C 选项", "D 选项"],
                    "answer_index": 1,
                    "explanation": "解析二",
                    "knowledge_point": "页面识别",
                    "difficulty_tag": "consolidate",
                    "is_image_based": True,
                    "citations": [{"ref": second}],
                },
                {
                    "ordinal": 3,
                    "stem": "图上直接连线说明了什么？",
                    "options": ["A 选项", "B 选项", "C 选项", "D 选项"],
                    "answer_index": 2,
                    "explanation": "解析三",
                    "knowledge_point": "连线识别",
                    "difficulty_tag": "consolidate",
                    "is_image_based": False,
                    "citations": [{"ref": first}],
                },
            ],
            "insufficient_evidence": None,
        }
        return ModelResult(
            content_text=json.dumps(output, ensure_ascii=False),
            finish_reason="stop",
            usage={},
            latency_ms=1,
        )


def create_pack(client, sources, key: str) -> str:
    response = client.post(
        "/api/packs",
        json={
            "idempotency_key": key,
            "sources": [
                {"source_id": item["id"], "page_start": 2, "page_end": 2}
                for item in sources
            ],
            "progress_text": "我刚开始学习，只想认清图中的设备和作用。",
            "difficulty": "consolidate",
            "question_count": 3,
            "consent_ack": True,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


@pytest.fixture
def pdf_cleanup(client):
    sources_to_clean = []
    packs_to_clean = []
    yield sources_to_clean, packs_to_clean
    for pack_id in packs_to_clean:
        client.request("DELETE", f"/api/packs/{pack_id}", json={"confirm": True})
    for source in sources_to_clean:
        client.request("DELETE", f"/api/sources/{source['id']}", json={"confirm": True})


def test_two_pdfs_page_two_send_unique_nonempty_page_images(client, tmp_path, monkeypatch, pdf_cleanup):
    sources_to_clean, packs_to_clean = pdf_cleanup
    pdf_a = tmp_path / "network_a.pdf"
    pdf_b = tmp_path / "network_b_scanned.pdf"
    make_pdf(pdf_a, 1, scanned_page_two=False)
    make_pdf(pdf_b, 2, scanned_page_two=True)

    source_a = upload(client, pdf_a)
    source_b = upload(client, pdf_b)
    sources_to_clean.extend([source_a, source_b])
    recorder = RecordingModel()
    monkeypatch.setattr(generation, "get_model_client", lambda: recorder)

    job_id = create_pack(client, [source_a, source_b], "pdf-page2-unique-01")
    job = wait_job(client, job_id)
    assert job["status"] == "succeeded", job
    assert len(recorder.requests) == 1
    request = recorder.requests[0]

    page_images = [image for image in request.images if image.ref.startswith("P")]
    assert [image.ref for image in page_images] == ["P1", "P2"]
    assert len({image.ref for image in page_images}) == 2
    for image in page_images:
        assert image.data
        with Image.open(io.BytesIO(image.data)) as decoded:
            assert decoded.width > 0 and decoded.height > 0

    payload = DeepSeekClient(api_key="test-key").build_payload(request)
    image_urls = [
        block["image_url"]["url"]
        for block in payload["messages"][1]["content"]
        if block["type"] == "image_url"
    ]
    assert len(image_urls) == len(request.images)
    for url in image_urls:
        assert url.startswith("data:image/")
        raw = base64.b64decode(url.split(",", 1)[1])
        assert raw
        with Image.open(io.BytesIO(raw)) as decoded:
            assert decoded.width > 0

    assert all("第 2 页" in item.description for item in request.text_items)

    source_names = {
        source_a["id"]: source_a["display_name"],
        source_b["id"]: source_b["display_name"],
    }
    with SessionLocal() as db:
        rows = db.execute(
            select(GenerationEvidence)
            .where(GenerationEvidence.job_id == job_id)
            .order_by(GenerationEvidence.ordinal)
        ).scalars().all()
        page_rows = [row for row in rows if row.local_ref.startswith("P")]
        assert {row.local_ref for row in page_rows} == {"P1", "P2"}
        assert len({row.source_id for row in page_rows}) == 2
        for row in rows:
            evidence = db.get(Evidence, row.evidence_id)
            assert evidence is not None
            assert evidence.page_no == 2
        for row in page_rows:
            evidence = db.get(Evidence, row.evidence_id)
            assert evidence.kind == "page_image"

        scanned = db.get(SourcePage, (source_b["id"], 2))
        assert scanned is not None and scanned.is_scanned is True
        vector = db.get(SourcePage, (source_a["id"], 2))
        assert vector is not None and vector.is_scanned is False

    pack_id = job["pack_id"]
    packs_to_clean.append(pack_id)
    detail = client.get(f"/api/packs/{pack_id}").json()
    assert detail["sources"][0]["page_start"] == 2
    assert detail["sources"][1]["page_end"] == 2

    submit = client.post(
        f"/api/packs/{pack_id}/submit",
        json={"idempotency_key": "pdf-page2-submit-01", "answers": []},
    )
    assert submit.status_code == 200, submit.text

    with SessionLocal() as db:
        citations = db.execute(
            select(Citation).where(
                Citation.pack_id == pack_id,
                Citation.local_ref.in_(["P1", "P2"]),
            )
        ).scalars().all()
        by_ref = {}
        for citation in citations:
            by_ref.setdefault(citation.local_ref, citation)
            assert citation.page_no == 2
            evidence = db.get(Evidence, citation.evidence_id)
            assert evidence is not None and evidence.page_no == 2
        assert set(by_ref) == {"P1", "P2"}

        ref_source = {row.local_ref: row.source_id for row in page_rows}
        for row in page_rows:
            citation = by_ref[row.local_ref]
            assert source_names[row.source_id] == source_names[citation.source_id]

    for ref, citation in by_ref.items():
        response = client.get(f"/api/citations/{citation.id}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["page_no"] == 2
        assert payload["source_name"] == source_names[ref_source[ref]]


def test_missing_page_image_fails_without_calling_model(client, tmp_path, monkeypatch, pdf_cleanup):
    sources_to_clean, _packs = pdf_cleanup
    pdf_a = tmp_path / "missing_page.pdf"
    make_pdf(pdf_a, 3, scanned_page_two=False)
    source = upload(client, pdf_a)
    sources_to_clean.append(source)
    recorder = RecordingModel()
    monkeypatch.setattr(generation, "get_model_client", lambda: recorder)

    with SessionLocal() as db:
        page = db.get(SourcePage, (source["id"], 2))
        assert page is not None and page.page_image_asset_id
        asset = db.get(ImageAsset, page.page_image_asset_id)
        assert asset is not None
        asset.rel_path = "derived/does-not-exist/p0002.png"
        db.commit()

    job_id = create_pack(client, [source], "pdf-page2-missing-01")
    job = wait_job(client, job_id)
    assert job["status"] == "failed"
    assert job["error"]["code"] == "source_unavailable"
    assert job["pack_id"] is None
    assert recorder.requests == []