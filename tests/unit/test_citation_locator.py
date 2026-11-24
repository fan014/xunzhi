from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app.api.routes import _locator_boxes
from app.domain.deletion import locator_valid
from app.domain.generation import _citation_locator
from app.domain.models import Citation
from app.domain.schemas import CitationRef, RefTarget

ROOT = Path(__file__).resolve().parents[2]


def target(kind: str, locator: dict) -> RefTarget:
    return RefTarget(
        evidence_id="ev_locator",
        source_id="src_locator",
        kind=kind,
        page_no=2 if kind == "text_span" else None,
        confidence="high",
        locator_json=json.dumps(locator, ensure_ascii=False),
        quote_text="证据摘要",
    )


def test_locator_boxes_supports_rects_and_box_2d():
    assert _locator_boxes({"rects": [[0.1, 0.2, 0.5, 0.6]]}) == [[0.1, 0.2, 0.5, 0.6]]
    assert _locator_boxes({"box_2d": [0.2, 0.2, 0.8, 0.8]}) == [[0.2, 0.2, 0.8, 0.8]]
    assert _locator_boxes({"boxes": [[0.0, 0.0, 0.4, 0.4]], "rect": [0.1, 0.1, 0.9, 0.9]}) == [
        [0.0, 0.0, 0.4, 0.4]
    ]


def test_generation_keeps_valid_image_box_and_description():
    parent = target("image_region", {"boxes": [[0.1, 0.1, 0.8, 0.8]], "asset_id": "ast"})
    citation = CitationRef(
        ref="I1",
        box_2d=[0.2, 0.2, 0.5, 0.5],
        image_description="图中可见 PC-A 与 SW1 的连线",
    )
    locator = json.loads(_citation_locator(parent, citation))
    assert locator["boxes"] == [[0.2, 0.2, 0.5, 0.5]]
    assert locator["box_2d"] == [0.2, 0.2, 0.5, 0.5]
    assert locator["image_description"] == "图中可见 PC-A 与 SW1 的连线"


def test_generation_does_not_persist_invalid_or_out_of_parent_boxes():
    parent = target("image_region", {"boxes": [[0.2, 0.2, 0.6, 0.6]]})
    outside = CitationRef(ref="I1", box_2d=[0.1, 0.1, 0.5, 0.5])
    locator = json.loads(_citation_locator(parent, outside))
    assert locator["boxes"] == [[0.2, 0.2, 0.6, 0.6]]
    assert "box_2d" not in locator

    inverted = CitationRef(ref="I1", box_2d=[0.5, 0.5, 0.2, 0.2])
    locator = json.loads(_citation_locator(parent, inverted))
    assert "box_2d" not in locator


def test_text_evidence_ignores_image_box_fields():
    text = target("text_span", {"rects": [[0.1, 0.2, 0.9, 0.3]]})
    citation = CitationRef(ref="T1", box_2d=[0.2, 0.2, 0.4, 0.4], image_description="不应写入")
    locator = json.loads(_citation_locator(text, citation))
    assert "box_2d" not in locator
    assert "image_description" not in locator


def test_locator_valid_checks_rects_and_box_2d():
    citation = Citation(id="c", pack_id="p", owner_type="section", owner_id="s", evidence_id="e", source_id="src")
    citation.locator_json = json.dumps({"rects": [[0.0, 0.0, 0.5, 0.5]]})
    assert locator_valid(citation) is True
    citation.locator_json = json.dumps({"box_2d": [0.5, 0.5, 0.2, 0.2]})
    assert locator_valid(citation) is False


def load_probe_module():
    spec = importlib.util.spec_from_file_location("smoke_real_citation_probe", ROOT / "scripts" / "smoke_real.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, content_type: str = "application/json"):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {"content-type": content_type}
        self.content = b"" if payload is None else b"{}"

    def json(self):
        return self._payload


class TextOnlyClient:
    def get(self, url):
        assert "/asset" not in url, "pure text citation must not fetch an image asset"
        return FakeResponse(
            200,
            {
                "status": "ok",
                "source_name": "note.txt",
                "kind": "text_span",
                "page_no": None,
                "quote": "交换机按 MAC 转发",
                "claim": "交换机按目的 MAC 转发",
                "boxes": [],
                "asset_expected": False,
                "asset_url": None,
                "text_alternative": "交换机按 MAC 转发",
                "text_alternative_source": "quote",
            },
        )


def test_smoke_does_not_check_asset_for_text_only_citation():
    module = load_probe_module()
    meta = module.citation_meta(TextOnlyClient(), {"id": "cit_text"}, {"note.txt"})
    assert meta["asset_expected"] is False
    assert meta["asset_openable"] is None
    assert meta["asset_status"] is None
    assert meta["claim"] == "交换机按目的 MAC 转发"