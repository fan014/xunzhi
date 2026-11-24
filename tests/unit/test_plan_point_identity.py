from __future__ import annotations

import pytest

from app.domain.generation import resolve_plan_points, validate_plan_delivery
from app.domain.plans import _ambiguous_plan_point_names
from app.domain.schemas import ModelOutput, ModelQuestion
from app.domain.validation import ValidationError


def _question(ordinal: int, name: str, point_id: str = "") -> ModelQuestion:
    return ModelQuestion(
        ordinal=ordinal,
        stem=f"第 {ordinal} 题：{name}",
        options=["甲", "乙", "丙", "丁"],
        answer_index=0,
        explanation="依据资料判断。",
        knowledge_point=name,
        knowledge_point_id=point_id,
        module_id="mod_a",
    )


def _output(questions: list[ModelQuestion]) -> ModelOutput:
    return ModelOutput(questions=questions)


def _items() -> list[dict]:
    return [
        {
            "knowledge_point_id": "kp_a",
            "display_name": "同名考点",
            "module_id": "mod_a",
            "source_id": "source_a",
            "suggested_question_count": 2,
        },
        {
            "knowledge_point_id": "kp_b",
            "display_name": "同名考点",
            "module_id": "mod_b",
            "source_id": "source_b",
            "suggested_question_count": 2,
        },
    ]


def test_same_name_questions_map_by_stable_id():
    output = _output(
        [
            _question(1, "同名考点", "kp_a"),
            _question(2, "同名考点", "kp_a"),
            _question(3, "同名考点", "kp_b"),
            _question(4, "同名考点", "kp_b"),
        ]
    )
    mapping = resolve_plan_points(None, output, {"items": _items()}, [], None)
    assert mapping == {1: "kp_a", 2: "kp_a", 3: "kp_b", 4: "kp_b"}
    counts = {}
    for point_id in mapping.values():
        counts[point_id] = counts.get(point_id, 0) + 1
    assert counts == {"kp_a": 2, "kp_b": 2}
    delivery = validate_plan_delivery(
        output,
        mapping,
        {"items": _items(), "total_question_count": 4},
    )
    assert delivery["actual"]["by_point"] == {"kp_a": 2, "kp_b": 2}
    assert delivery["shortfall"] == []
    assert delivery["extra"] == []


def test_legacy_name_only_matches_when_unique():
    items = [
        {
            "knowledge_point_id": "kp_unique",
            "display_name": "唯一考点",
            "module_id": "mod_a",
            "source_id": "source_a",
            "suggested_question_count": 2,
        }
    ]
    output = _output([_question(1, "唯一考点"), _question(2, "唯一考点")])
    mapping = resolve_plan_points(None, output, {"items": items}, [], None)
    assert mapping == {1: "kp_unique", 2: "kp_unique"}


def test_legacy_name_only_with_duplicate_names_is_ambiguous():
    output = _output([_question(1, "同名考点")])
    with pytest.raises(ValidationError) as info:
        resolve_plan_points(None, output, {"items": _items()}, [], None)
    assert info.value.code == "plan_point_ambiguous"
    assert "重新制定学习计划" in info.value.message


def test_point_id_outside_current_batch_is_rejected():
    output = _output([_question(1, "同名考点", "kp_other")])
    with pytest.raises(ValidationError) as info:
        resolve_plan_points(None, output, {"items": _items()}, [], None)
    assert info.value.code == "plan_point_mismatch"


def test_excluded_point_id_is_rejected():
    output = _output([_question(1, "同名考点", "kp_a")])
    with pytest.raises(ValidationError) as info:
        resolve_plan_points(
            None, output, {"items": _items(), "excluded": ["kp_a"]}, [], None
        )
    assert info.value.code == "plan_excluded_point"


def test_preflight_ambiguity_detection():
    rows = [
        {
            "knowledge_point_id": "kp_a",
            "display_name": "同名考点",
            "module_id": "mod_a",
            "source_id": "source_a",
        },
        {
            "knowledge_point_id": "kp_b",
            "display_name": "同名考点",
            "module_id": "mod_b",
            "source_id": "source_b",
        },
    ]
    assert _ambiguous_plan_point_names(rows) is False
    legacy = [
        {"knowledge_point_id": None, "display_name": "同名考点", "module_id": None},
        {"knowledge_point_id": None, "display_name": "同名考点", "module_id": None},
    ]
    assert _ambiguous_plan_point_names(legacy) is True