from __future__ import annotations

from app.domain.generation import Assembly, _merge_batches
from app.domain.schemas import ModelOutput, RefMap, RefTarget


def _assembly(refs: list[tuple[str, str]]) -> Assembly:
    assembly = Assembly()
    for ref, kind in refs:
        assembly.ref_map.targets[ref] = RefTarget(
            evidence_id=f"ev_{ref}",
            source_id="src_test",
            kind=kind,
            page_no=2 if kind != "text_span" else None,
            confidence="high",
            locator_json="{}",
            quote_text="原文",
        )
    return assembly


def _output(module_id: str, page_id: str, stem: str) -> ModelOutput:
    return ModelOutput.model_validate(
        {
            "handout": {
                "scope": {"covered": ["覆盖"], "not_covered": ["不覆盖"]},
                "key_points": [
                    {"heading": "机制", "body": "正文", "citations": [{"ref": "T1"}]}
                ],
                "pitfalls": [
                    {"heading": "易混淆", "body": "正文", "citations": [{"ref": "T1"}]}
                ],
            },
            "modules": [
                {
                    "module_id": module_id,
                    "title": module_id,
                    "core_points": ["考点"],
                    "key_points": [
                        {"heading": "机制", "body": "正文", "citations": [{"ref": "T1"}]}
                    ],
                    "pitfalls": [
                        {"heading": "易混淆", "body": "正文", "citations": [{"ref": "T1"}]}
                    ],
                    "pages": [
                        {
                            "page_id": page_id,
                            "knowledge_point_ids": ["kp_1"],
                            "title": "资料图",
                            "explanation": "本页",
                            "visual": {"type": "source_asset", "source_ref": "I1"},
                            "related_sources": [
                                {"ref": "I1", "claim": "图"},
                                {"ref": "T1", "claim": "文"},
                            ],
                        }
                    ],
                    "recap": {
                        "summary": "回顾",
                        "rules": [
                            {"condition": "条件", "result": "结果", "pitfall": "误区"}
                        ],
                    },
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


def test_merge_batches_keeps_pages_and_rewrites_refs():
    constraints = {
        "modules": [
            {"module_id": "mod_1", "title": "模块一"},
            {"module_id": "mod_2", "title": "模块二"},
        ]
    }
    first = (
        1,
        _output("mod_1", "page-a", "第一题"),
        _assembly([("T1", "text_span"), ("I1", "image_region")]),
    )
    second = (
        2,
        _output("mod_2", "page-b", "第二题"),
        _assembly([("T1", "text_span"), ("I1", "image_region")]),
    )
    merged, assembly = _merge_batches([first, second], constraints)
    assert [module.module_id for module in merged.modules] == ["mod_1", "mod_2"]
    first_page = merged.modules[0].pages[0]
    second_page = merged.modules[1].pages[0]
    assert first_page.visual.type == "source_asset"
    assert second_page.visual.type == "source_asset"
    assert first_page.visual.source_ref != second_page.visual.source_ref
    assert first_page.visual.source_ref in assembly.ref_map.targets
    assert second_page.visual.source_ref in assembly.ref_map.targets
    refs = {citation.ref for citation in first_page.related_sources}
    assert first_page.visual.source_ref in refs
    assert all(citation.ref in assembly.ref_map.targets for citation in first_page.related_sources)
    assert first_page.page_id == "page-a"
    assert second_page.page_id == "page-b"
    assert merged.modules[0].recap.rules


def test_merge_batches_keeps_module_order_by_batch_index():
    constraints = {
        "modules": [
            {"module_id": "mod_1", "title": "模块一"},
            {"module_id": "mod_2", "title": "模块二"},
        ]
    }
    second = (
        2,
        _output("mod_2", "page-b", "第二题"),
        _assembly([("T1", "text_span"), ("I1", "image_region")]),
    )
    first = (
        1,
        _output("mod_1", "page-a", "第一题"),
        _assembly([("T1", "text_span"), ("I1", "image_region")]),
    )
    merged, _ = _merge_batches([second, first], constraints)
    assert [(module.module_id, module.pages[0].page_id) for module in merged.modules] == [
        ("mod_2", "page-b"),
        ("mod_1", "page-a"),
    ]