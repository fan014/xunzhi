from __future__ import annotations

import json

from app.domain.deletion import locator_valid
from app.domain.models import Citation


def make_citation(locator: dict) -> Citation:
    return Citation(
        id="cit_test",
        pack_id="pack_test",
        owner_type="section",
        owner_id="sec_test",
        evidence_id="ev_test",
        source_id="src_test",
        locator_json=json.dumps(locator),
    )


def test_locator_accepts_valid_boxes():
    assert locator_valid(make_citation({"boxes": [[0.1, 0.1, 0.5, 0.5]]})) is True


def test_locator_rejects_out_of_range_and_inverted_boxes():
    assert locator_valid(make_citation({"boxes": [[0.1, 0.1, 1.2, 0.5]]})) is False
    assert locator_valid(make_citation({"boxes": [[0.6, 0.1, 0.2, 0.5]]})) is False


def test_locator_rejects_broken_json():
    citation = make_citation({})
    citation.locator_json = "{not json"
    assert locator_valid(citation) is False