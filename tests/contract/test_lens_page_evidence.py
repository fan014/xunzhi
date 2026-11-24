from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from app.domain.generation import assemble_for_sources
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers

LENS_PDF = Path(__file__).resolve().parent.parent.parent / "samples" / "lens_two_conditions.pdf"


def test_lens_pdf_sends_two_regions_for_two_conditions(client, tmp_path):
    assert LENS_PDF.exists(), "请先运行 python scripts/make_lens_acceptance_pdf.py"
    source = helpers.upload(client, LENS_PDF)
    with SessionLocal() as db:
        assembly = assemble_for_sources(
            db,
            [{"source_id": source["id"], "page_start": 1, "page_end": 2}],
            load_images=True,
            text_limit=20,
            image_limit=2,
            balanced=True,
        )
    refs = set(assembly.ref_map.targets)
    assert {"I1", "I2"}.issubset(refs), refs
    descriptions = {item.ref: item.description for item in assembly.images}
    assert "第 1 页" in descriptions["I1"]
    assert "第 2 页" in descriptions["I2"]
    assert assembly.images[0].data != assembly.images[1].data
    for item in assembly.images:
        with Image.open(io.BytesIO(item.data)) as decoded:
            assert decoded.width > 0 and decoded.height > 0
    assert assembly.ref_map.get("I1").page_no == 1
    assert assembly.ref_map.get("I2").page_no == 2


def test_region_crop_is_smaller_than_full_page_image():
    from app.infra import image_engine

    full = Image.new("RGB", (800, 1000), "white")
    buffer = io.BytesIO()
    full.save(buffer, format="PNG")
    data = buffer.getvalue()
    cropped = image_engine.crop_region(data, [0.1, 0.2, 0.5, 0.6])
    with Image.open(io.BytesIO(cropped)) as decoded:
        assert decoded.width == 320
        assert decoded.height == 400
    assert image_engine.crop_region(data, [0.6, 0.2, 0.5, 0.6]) == data
    assert image_engine.crop_region(data, [0, 0, 1, 1]) != data