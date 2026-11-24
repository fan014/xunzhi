from __future__ import annotations

import pytest

from app.domain.generation import Assembly, BatchConflict, _merge_batches
from app.domain.schemas import ModelOutput, RefMap, RefTarget


def _output(stem: str, module_id: str) -> ModelOutput:
    block = {
        "heading": "要点",
        "body": "讲义正文" * 50,
        "citations": [{"ref": "T1"}],
    }
    return ModelOutput.model_validate(
        {
            "progress_reading": {},
            "handout": {
                "scope": {"covered": ["覆盖"], "not_covered": ["不覆盖"]},
                "key_points": [block],
                "pitfalls": [block],
                "visual": [],
            },
            "modules": [
                {
                    "module_id": module_id,
                    "title": module_id,
                    "core_points": ["考点"],
                    "key_points": [block],
                    "pitfalls": [block],
                }
            ],
            "questions": [
                {
                    "ordinal": 1,
                    "stem": stem,
                    "options": ["A", "B", "C", "D"],
                    "answer_index": 0,
                    "explanation": "解析",
                    "knowledge_point": "考点",
                    "module_id": module_id,
                    "citations": [{"ref": "T1"}],
                }
            ],
        }
    )


def _assembly() -> Assembly:
    return Assembly(
        ref_map=RefMap(
            {
                "T1": RefTarget(
                    evidence_id="ev_test",
                    source_id="src_test",
                    kind="text_span",
                    page_no=None,
                    confidence="high",
                    locator_json='{"chars":[0,4]}',
                    quote_text="正文",
                    label_text=None,
                )
            }
        ),
        records=[],
    )


def test_duplicate_stem_points_to_later_batch():
    constraints = {
        "modules": [
            {"module_id": "mod_1", "title": "模块1"},
            {"module_id": "mod_2", "title": "模块2"},
        ]
    }
    with pytest.raises(BatchConflict) as excinfo:
        _merge_batches(
            [
                (1, _output("同一道题干", "mod_1"), _assembly()),
                (2, _output("同一道题干", "mod_2"), _assembly()),
            ],
            constraints,
        )
    assert excinfo.value.batch_index == 2


def test_distinct_stems_merge_in_order():
    constraints = {"modules": [{"module_id": "mod_1", "title": "模块1"}]}
    merged, assembly = _merge_batches(
        [(1, _output("第一题题干", "mod_1"), _assembly())],
        constraints,
    )
    assert [question.ordinal for question in merged.questions] == [1]
    assert assembly.ref_map.get("T1") is not None