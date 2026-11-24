from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pymupdf
from PIL import Image
from sqlalchemy import select

from app.domain import generation
from app.domain.models import Evidence, GenerationEvidence
from app.infra.db import SessionLocal
from app.infra.model_client import ModelResult

ROOT = Path(__file__).resolve().parents[2]


def load_probe_module():
    spec = importlib.util.spec_from_file_location("smoke_real_review_probe", ROOT / "scripts" / "smoke_real.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ReviewRecordingModel:
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        page_refs = [image.ref for image in request.images if image.ref.startswith("P")]
        assert len(page_refs) >= 2, page_refs
        first, second = page_refs[0], page_refs[1]
        key = [
            {"heading": "要点一", "body": "正文一", "role": "weak", "citations": [{"ref": first}]},
            {"heading": "要点二", "body": "正文二", "role": "weak", "citations": [{"ref": second}]},
            {"heading": "要点三", "body": "正文三", "role": "neutral", "citations": [{"ref": first}]},
        ]
        pitfalls = [
            {"heading": "易错一", "body": "错误一", "role": "weak", "citations": [{"ref": first}]},
            {"heading": "易错二", "body": "错误二", "role": "weak", "citations": [{"ref": second}]},
        ]
        visual = [
            {"heading": "看图", "body": "图中独有标签与连线", "role": "weak", "citations": [{"ref": second}]}
        ]
        questions = []
        for ordinal in range(1, 6):
            questions.append(
                {
                    "ordinal": ordinal,
                    "stem": f"第 {ordinal} 题：观察图中独有标签或连线后作答",
                    "options": ["A 选项", "B 选项", "C 选项", "D 选项"],
                    "answer_index": (ordinal - 1) % 4,
                    "explanation": f"第 {ordinal} 题解析",
                    "knowledge_point": "设备识别",
                    "difficulty_tag": "consolidate",
                    "is_image_based": ordinal <= 3,
                    "citations": [{"ref": first if ordinal % 2 else second}],
                }
            )
        output = {
            "progress_reading": {
                "learned": ["交换机"],
                "unlearned": [],
                "weak_points": ["设备作用"],
                "intent": ["读图练习"],
                "insufficient": False,
            },
            "handout": {
                "scope": {"covered": ["设备身份与作用、直接连线"], "not_covered": ["路由协议细节"]},
                "key_points": key,
                "pitfalls": pitfalls,
                "visual": visual,
            },
            "questions": questions,
            "insufficient_evidence": None,
        }
        return ModelResult(
            content_text=json.dumps(output, ensure_ascii=False),
            finish_reason="stop",
            usage={},
            latency_ms=1,
        )


def cleanup(client, packs, sources):
    for pack_id in packs:
        if pack_id:
            client.request("DELETE", f"/api/packs/{pack_id}", json={"confirm": True})
    for source in sources:
        client.request("DELETE", f"/api/sources/{source['id']}", json={"confirm": True})


def test_review_assets_cover_pdf_pages_12_to_28_and_baseline_labels(tmp_path):
    module = load_probe_module()
    pdf_path, png_path = module.generate_review_assets(tmp_path)
    doc = pymupdf.open(pdf_path)
    try:
        assert doc.page_count == 28
        device_page = doc[27]
        assert device_page.get_text().strip() == ""
        assert device_page.get_drawings()
        assert not device_page.get_images()
        text = "\n".join(doc[index].get_text() for index in range(11, 27))
    finally:
        doc.close()
    for token in ("Device roles", "Default gateway", "MAC table", "Local address resolution", "Different subnet"):
        assert token in text
    assert "routing protocol" not in text.lower()
    for token in ("PC-A", "PC-B", "PC-C", "SW1", "R1"):
        assert token not in text
    first = pymupdf.open(pdf_path)
    try:
        assert "Scope-excluded" in first[0].get_text()
    finally:
        first.close()
    with Image.open(png_path) as image:
        assert image.size == (1000, 620)


def test_review_mode_flags_keep_old_ab_question_count(monkeypatch):
    module = load_probe_module()
    monkeypatch.setattr(sys, "argv", ["smoke_real.py", "--ab"])
    old_args = module.parse_args()
    assert old_args.ab is True and old_args.review is False
    assert module.question_count_for(old_args) == 3

    monkeypatch.setattr(sys, "argv", ["smoke_real.py", "--ab", "--review"])
    review_args = module.parse_args()
    assert review_args.ab is True and review_args.review is True
    assert module.question_count_for(review_args) == 5


def test_review_ab_generates_five_questions_and_maps_evidence(client, tmp_path, monkeypatch):
    module = load_probe_module()
    packs = []
    sources = []
    try:
        sources, page_ranges = module.review_sources(client, tmp_path)
        recorder = ReviewRecordingModel()
        monkeypatch.setattr(generation, "get_model_client", lambda: recorder)

        run_a = module.evaluate(
            client,
            sources,
            module.PROGRESS_A,
            30,
            question_count=5,
            page_ranges=page_ranges,
            require_image_question=True,
        )
        run_b = module.evaluate(
            client,
            sources,
            module.PROGRESS_B,
            30,
            question_count=5,
            page_ranges=page_ranges,
            require_image_question=True,
        )
        assert run_a["status"] == "succeeded", run_a
        assert run_b["status"] == "succeeded", run_b
        for run in (run_a, run_b):
            assert run["question_count"] == 5
            assert len(run["questions"]) == 5
            assert all(item["is_image_based"] is not None for item in run["questions"])
            assert run["checks"]["question_count_match"] is True
            assert run["checks"]["assets_openable"] is True
            assert run["checks"]["has_image_question"] is True
            assert run["checks"]["image_question_has_image_ref"] is True
            assert any(citation["type"] in {"page_image", "image_region"} for q in run["questions"] for citation in q["citations"])
            pdf_source = next(item for item in run["sources"] if item["page_start"] == 12)
            assert pdf_source["page_end"] == 28
        packs.extend([run_a["pack_id"], run_b["pack_id"]])

        assert len(recorder.requests) == 2
        for request in recorder.requests:
            page_images = [image for image in request.images if image.ref.startswith("P")]
            assert page_images and all(image.data for image in page_images)
            assert len({image.ref for image in page_images}) == len(page_images)

        with SessionLocal() as db:
            for run in (run_a, run_b):
                rows = db.execute(
                    select(GenerationEvidence).where(GenerationEvidence.job_id == run["job_id"])
                ).scalars().all()
                assert rows
                for row in rows:
                    evidence = db.get(Evidence, row.evidence_id)
                    assert evidence is not None
                    if evidence.source_id == sources[0]["id"]:
                        assert 12 <= evidence.page_no <= 28
    finally:
        cleanup(client, packs, sources)

def sample_run(prefix: str, types: list[str]) -> dict:
    handout = []
    questions = []
    for index, kind in enumerate(types, start=1):
        citation = {
            "citation_id": f"{prefix}_h{index}",
            "type": kind,
            "source_name": "review_topology.png" if kind == "image_region" else "review_handbook.pdf",
            "page_no": 28 if kind == "page_image" else None,
            "quote": "证据摘要",
            "claim": "引用结论",
            "image_description": "图中可见的标签或连线",
            "boxes": [[0.1, 0.1, 0.5, 0.5]] if kind != "text_span" else [],
            "asset_openable": True,
            "asset_status": 200,
            "asset_mime": "image/png" if kind != "text_span" else None,
            "asset_bytes": 100,
        }
        handout.append({
            "section_type": "key_point",
            "heading": f"节 {index}",
            "body": f"节 {index} 正文",
            "citations": [citation],
        })
        question_citation = {**citation, "citation_id": f"{prefix}_q{index}"}
        questions.append({
            "ordinal": index,
            "stem": f"第 {index} 题",
            "explanation": "解析",
            "citations": [question_citation],
        })
    return {"status": "succeeded", "handout": handout, "questions": questions}


def test_l3_sample_is_same_pack_five_plus_five_and_three_types():
    module = load_probe_module()
    run_a = sample_run("A", ["text_span", "page_image", "image_region", "text_span", "page_image"])
    run_b = sample_run("B", ["text_span", "page_image", "image_region", "page_image", "text_span"])
    for prefix, run in (("A", run_a), ("B", run_b)):
        sample = module.build_l3_sample(run)
        assert sample["ready"] is True
        assert len(sample["items"]) == 10
        assert sum(1 for item in sample["items"] if item["bucket"] == "handout") == 5
        assert sum(1 for item in sample["items"] if item["bucket"] == "question") == 5
        assert {item["type"] for item in sample["items"]} >= {"text_span", "page_image", "image_region"}
        assert len({item["citation_id"] for item in sample["items"]}) == 10
        assert all(item["citation_id"].startswith(prefix + "_") for item in sample["items"])
        assert all(item["claim"] == "引用结论" for item in sample["items"])
        handout_items = [item for item in sample["items"] if item["bucket"] == "handout"]
        assert all(item["context"]["body"].endswith("正文") for item in handout_items)


def test_l3_sample_reports_missing_type_without_filling():
    module = load_probe_module()
    run = sample_run("A", ["page_image"] * 10)
    sample = module.build_l3_sample(run)
    assert sample["ready"] is False
    assert any("text_span" in reason for reason in sample["missing"])
    assert any("image_region" in reason for reason in sample["missing"])


def test_l3_sample_rejects_p12_text_page_when_p28_missing():
    module = load_probe_module()
    run = sample_run("A", ["text_span", "page_image", "image_region", "text_span", "page_image"])
    for section in run["handout"]:
        for citation in section["citations"]:
            if citation["type"] == "page_image":
                citation["page_no"] = 12
    for question in run["questions"]:
        for citation in question["citations"]:
            if citation["type"] == "page_image":
                citation["page_no"] = 12
    sample = module.build_l3_sample(run)
    assert sample["ready"] is False
    assert any("page_image" in reason and "28" in reason for reason in sample["missing"])
