from __future__ import annotations

import json

from sqlalchemy import select

from app.domain.models import Citation, PackSource, Question
from app.infra.db import SessionLocal
from tests.stage4_helpers import generate_pack


def test_generated_pack_satisfies_structural_rules(client, tmp_path):
    _, pack = generate_pack(client, tmp_path)
    assert len(pack["questions"]) == 3
    ordinals = [item["ordinal"] for item in pack["questions"]]
    assert ordinals == [1, 2, 3]
    for question in pack["questions"]:
        options = question["options"]
        assert len(options) == 4
        texts = {item["text"] for item in options}
        assert len(texts) == 4
        assert [item["key"] for item in options] == ["A", "B", "C", "D"]
        assert "OSPF" not in question["stem"]
        assert "路由表" not in question["stem"]


def test_every_question_and_handout_section_has_citation_in_pack_scope(client, tmp_path):
    _, pack = generate_pack(client, tmp_path)
    with SessionLocal() as db:
        links = db.execute(select(PackSource).where(PackSource.pack_id == pack["id"])).scalars().all()
        source_ids = {link.source_id for link in links}
        page_ranges = {
            link.source_id: (link.page_start, link.page_end)
            for link in links
            if link.page_start is not None
        }
        citations = db.execute(select(Citation).where(Citation.pack_id == pack["id"])).scalars().all()
        question_ids = {
            row.id for row in db.execute(select(Question).where(Question.pack_id == pack["id"])).scalars().all()
        }
        assert citations
        for citation in citations:
            assert citation.source_id in source_ids
            assert citation.evidence_id
            if citation.page_no is not None and citation.source_id in page_ranges:
                start, end = page_ranges[citation.source_id]
                assert start <= citation.page_no <= end
            locator = json.loads(citation.locator_json)
            boxes = locator.get("boxes") or ([locator["rect"]] if locator.get("rect") else [])
            for box in boxes:
                assert all(0 <= value <= 1 for value in box)

        for question in pack["questions"]:
            assert question["id"] in question_ids

    handout = [item for item in pack["handout"] if item["section_type"] != "scope"]
    assert handout
    assert all(item["citations"] for item in handout)