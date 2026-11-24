from __future__ import annotations

from app.domain.generation import assemble_for_sources
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def test_balanced_image_budget_is_shared_across_page_and_region_images(client, sample_pdf, topology_png):
    pdf = helpers.upload(client, sample_pdf)
    image = helpers.upload(client, topology_png)
    specs = [
        {"source_id": pdf["id"], "page_start": None, "page_end": None},
        {"source_id": image["id"], "page_start": None, "page_end": None},
    ]
    with SessionLocal() as db:
        assembly = assemble_for_sources(
            db,
            specs,
            load_images=True,
            text_limit=10,
            image_limit=2,
            balanced=True,
        )

    assert len(assembly.images) == 2
    image_sources = {
        target.source_id
        for target in assembly.ref_map.targets.values()
        if target.kind in {"page_image", "image_region"}
    }
    assert image_sources == {pdf["id"], image["id"]}
    assert all(item.data for item in assembly.images)


def test_balanced_page_sampling_covers_each_source_and_last_page(client, sample_pdf, tmp_path):
    pdf = helpers.upload(client, sample_pdf)
    note = tmp_path / "balanced.txt"
    note.write_text("第一段证据。\n\n第二段证据。\n\n第三段证据。", encoding="utf-8")
    text = helpers.upload(client, note)
    specs = [
        {"source_id": pdf["id"], "page_start": None, "page_end": None},
        {"source_id": text["id"], "page_start": None, "page_end": None},
    ]
    with SessionLocal() as db:
        assembly = assemble_for_sources(
            db,
            specs,
            load_images=False,
            text_limit=4,
            image_limit=0,
            balanced=True,
        )

    text_sources = {
        target.source_id for target in assembly.ref_map.targets.values() if target.kind == "text_span"
    }
    assert text_sources == {pdf["id"], text["id"]}
    pdf_pages = {
        target.page_no
        for target in assembly.ref_map.targets.values()
        if target.source_id == pdf["id"] and target.page_no is not None
    }
    assert pdf_pages
    assert max(pdf_pages) >= 2