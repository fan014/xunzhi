from __future__ import annotations

import pytest

from app.domain.generation import Assembly, _require_source_asset_pages
from app.domain.schemas import ModelOutput, RefMap, RefTarget
from app.domain.validation import ValidationError, sanitize_module_pages


def _ref_map() -> RefMap:
    refs = RefMap()
    for ref, kind in (
        ("T1", "text_span"),
        ("P1", "page_image"),
        ("P2", "page_image"),
    ):
        refs.targets[ref] = RefTarget(
            evidence_id=f"ev_{ref}",
            source_id="src_test",
            kind=kind,
            page_no=1 if kind == "page_image" else None,
            confidence="high",
            locator_json="{}",
            quote_text="资料原文",
        )
    return refs


def _output(page: dict, module_id: str = "mod_1") -> ModelOutput:
    return ModelOutput.model_validate(
        {
            "modules": [
                {
                    "module_id": module_id,
                    "title": "模块",
                    "objective": "目标",
                    "core_points": ["考点"],
                    "key_points": [
                        {"heading": "机制", "body": "正文" * 40, "citations": [{"ref": "T1"}]}
                    ],
                    "pitfalls": [
                        {"heading": "易混淆", "body": "正文" * 40, "citations": [{"ref": "T1"}]}
                    ],
                    "pages": [page],
                }
            ]
        }
    )


def _base_page(**extra) -> dict:
    page = {
        "page_id": "p1",
        "knowledge_point_ids": ["kp_1"],
        "title": "学习页",
        "explanation": "本页讲解",
        "related_sources": [],
    }
    page.update(extra)
    return page


def test_comparison_page_is_kept():
    page = _base_page(
        visual={
            "type": "comparison",
            "title": "条件对比",
            "cards": [
                {"title": "条件一", "condition": "甲", "result": "结果一"},
                {"title": "条件二", "condition": "乙", "result": "结果二"},
            ],
        }
    )
    output = _output(page)
    warnings = sanitize_module_pages(output, _ref_map(), {"P1", "P2"})
    assert output.modules[0].pages[0].visual.type == "comparison"
    assert warnings == []


def test_source_asset_dropped_ref_is_downgraded():
    output = _output(
        _base_page(visual={"type": "source_asset", "source_ref": "P2"}),
    )
    warnings = sanitize_module_pages(output, _ref_map(), {"P1"})
    visual = output.modules[0].pages[0].visual
    assert visual.type == "text"
    assert "暂不可用" in visual.notice
    assert warnings


def test_source_asset_unknown_ref_is_downgraded():
    output = _output(
        _base_page(visual={"type": "source_asset", "source_ref": "P9"}),
    )
    sanitize_module_pages(output, _ref_map(), {"P1", "P2"})
    assert output.modules[0].pages[0].visual.type == "text"


def test_source_asset_kept_when_sent():
    output = _output(
        _base_page(visual={"type": "source_asset", "source_ref": "P1"}),
    )
    warnings = sanitize_module_pages(output, _ref_map(), {"P1", "P2"})
    assert output.modules[0].pages[0].visual.type == "source_asset"
    assert warnings == []


def test_invalid_step_reference_is_downgraded():
    output = _output(
        _base_page(
            visual={
                "type": "step_sequence",
                "nodes": [{"id": "n1", "label": "甲"}],
                "edges": [],
                "steps": [
                    {
                        "text": "第一步",
                        "active_node_ids": ["n1", "missing"],
                        "active_edge_ids": [],
                    }
                ],
            }
        )
    )
    warnings = sanitize_module_pages(output, _ref_map(), {"P1", "P2"})
    assert output.modules[0].pages[0].visual.type == "text"
    assert warnings


def test_invalid_answer_option_is_downgraded():
    output = _output(
        _base_page(
            visual={
                "type": "step_sequence",
                "nodes": [{"id": "n1", "label": "甲"}],
                "edges": [],
                "options": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}],
                "modes": [
                    {
                        "id": "m1",
                        "label": "场景",
                        "correct_option_id": "missing",
                        "steps": [{"text": "第一步", "active_node_ids": ["n1"]}],
                    }
                ],
            }
        )
    )
    sanitize_module_pages(output, _ref_map(), {"P1", "P2"})
    assert output.modules[0].pages[0].visual.type == "text"


def test_valid_step_sequence_is_kept():
    output = _output(
        _base_page(
            visual={
                "type": "step_sequence",
                "title": "分步",
                "nodes": [{"id": "n1", "label": "甲"}, {"id": "n2", "label": "乙"}],
                "edges": [{"id": "e1", "source": "n1", "target": "n2", "label": ""}],
                "steps": [
                    {
                        "text": "第一步",
                        "active_node_ids": ["n1"],
                        "active_edge_ids": ["e1"],
                        "node_states": {"n1": "进行中"},
                    }
                ],
            }
        )
    )
    warnings = sanitize_module_pages(output, _ref_map(), {"P1", "P2"})
    assert output.modules[0].pages[0].visual.type == "step_sequence"
    assert warnings == []


def test_duplicate_page_id_is_rebuilt():
    output = ModelOutput.model_validate(
        {
            "modules": [
                {
                    "module_id": "mod_1",
                    "title": "模块",
                    "pages": [
                        _base_page(page_id="same"),
                        _base_page(page_id="same"),
                    ],
                }
            ]
        }
    )
    sanitize_module_pages(output, _ref_map(), {"P1", "P2"})
    page_ids = [page.page_id for page in output.modules[0].pages]
    assert len(set(page_ids)) == 2

def _two_page_assembly() -> Assembly:
    assembly = Assembly()
    for ref, page in (("I1", 1), ("I2", 2)):
        assembly.ref_map.targets[ref] = RefTarget(
            evidence_id=f"ev_{ref}",
            source_id="src_test",
            kind="image_region",
            page_no=page,
            confidence="high",
            locator_json="{}",
            quote_text="资料图",
        )
    return assembly


def _visual_pages(refs: list[str]) -> ModelOutput:
    return ModelOutput.model_validate(
        {
            "modules": [
                {
                    "module_id": "mod_1",
                    "title": "透镜",
                    "pages": [
                        _base_page(
                            page_id=f"p-{index}",
                            visual={"type": "source_asset", "source_ref": ref},
                        )
                        for index, ref in enumerate(refs, start=1)
                    ],
                }
            ]
        }
    )


def _constraints() -> dict:
    return {
        "modules": [
            {
                "module_id": "mod_1",
                "title": "透镜",
                "source_ids": ["src_test"],
                "page_start": None,
                "page_end": None,
            }
        ]
    }


def test_comparison_requires_one_source_asset_page_per_image_page():
    output = _visual_pages(["I1"])
    with pytest.raises(ValidationError) as excinfo:
        _require_source_asset_pages(
            output,
            _constraints(),
            _two_page_assembly(),
            {"I1", "I2"},
            {"progress_text": "请对照两页透镜原图学习"},
        )
    assert excinfo.value.code == "module_source_asset_missing"


def test_comparison_accepts_source_asset_pages_for_both_pages():
    output = _visual_pages(["I1", "I2"])
    _require_source_asset_pages(
        output,
        _constraints(),
        _two_page_assembly(),
        {"I1", "I2"},
        {"progress_text": "请对照两页透镜原图学习"},
    )


def test_non_comparison_request_does_not_require_two_figure_pages():
    output = _visual_pages(["I1"])
    _require_source_asset_pages(
        output,
        _constraints(),
        _two_page_assembly(),
        {"I1", "I2"},
        {"progress_text": "练习交换机转发"},
    )