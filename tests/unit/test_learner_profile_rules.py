from __future__ import annotations

from types import SimpleNamespace

from app.domain import plans


def _item(point_id: str, state: str, *, possible: bool = False) -> dict:
    findings = (
        [{"kind": "possible_misunderstanding", "judgement": "可能误解"}]
        if possible
        else [{"kind": "observation", "judgement": "本轮答对"}]
    )
    return {
        "point": SimpleNamespace(id=point_id, display_name=point_id),
        "state": state,
        "mastery": SimpleNamespace(evidence_strength="weak"),
        "is_goal": False,
        "previous": {},
        "assessment_findings": findings,
        "assessment_routes": [{"action": "new_question"}],
        "assessment_focus": True,
    }


def test_progressed_points_drop_to_one_question_and_rest_is_reallocated():
    ranked = [
        _item("cross", "consolidating", possible=True),
        _item("compare", "consolidating", possible=True),
        _item("same", "stable"),
    ]
    items, unplaced = plans._allocate(ranked, 5, set())
    counts = {item["point"].id: item["count"] for item in items}
    assert counts["cross"] == 1
    assert counts["compare"] == 1
    assert counts["same"] == 3
    assert sum(counts.values()) == 5
    assert unplaced == []


def test_needs_reinforce_is_not_capped_at_one():
    ranked = [_item("cross", "needs_reinforce", possible=True)]
    items, _ = plans._allocate(ranked, 5, set())
    assert items[0]["count"] >= 2
    assert items[0]["count"] == 5