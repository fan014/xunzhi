from __future__ import annotations

import pytest

from app.domain.schemas import ModelOutput, RefMap, RefTarget
from app.domain.validation import ValidationError, validate_output


def ref_map() -> RefMap:
    return RefMap(
        {
            "F1": RefTarget(
                evidence_id="ev_1",
                source_id="src_1",
                kind="image_region",
                page_no=None,
                confidence="high",
                locator_json="{}",
                quote_text="拓扑图",
            ),
            "T1": RefTarget(
                evidence_id="ev_2",
                source_id="src_1",
                kind="text_span",
                page_no=2,
                confidence="high",
                locator_json="{}",
                quote_text="交换机按 MAC 转发",
            ),
        }
    )


def valid_payload(question_count: int = 3) -> dict:
    key = {
        "heading": "要点",
        "body": "正文",
        "role": "weak",
        "citations": [{"ref": "F1", "claim": "依据"}],
    }
    questions = []
    for index in range(1, question_count + 1):
        questions.append(
            {
                "ordinal": index,
                "stem": f"第 {index} 题的题干内容",
                "options": ["选项一", "选项二", "选项三", "选项四"],
                "answer_index": index % 4,
                "explanation": "解析说明了正确与错误原因",
                "knowledge_point": "考点",
                "citations": [{"ref": "T1", "claim": "依据"}],
            }
        )
    return {
        "progress_reading": {},
        "handout": {
            "scope": {"covered": ["a"], "not_covered": ["b"]},
            "key_points": [key, key, key],
            "pitfalls": [key, key],
            "visual": [key],
        },
        "questions": questions,
    }


def test_valid_output_passes():
    validate_output(ModelOutput.model_validate(valid_payload()), 3, ref_map(), True)


def test_question_count_mismatch():
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(valid_payload(2)), 3, ref_map(), True)
    assert info.value.code == "question_count_mismatch"


def test_missing_citation():
    payload = valid_payload()
    payload["questions"][0]["citations"] = []
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)
    assert info.value.code == "missing_citation"


def test_unknown_reference():
    payload = valid_payload()
    payload["questions"][1]["citations"] = [{"ref": "F99", "claim": "越界"}]
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)
    assert info.value.code == "citation_not_found"


def test_answer_index_out_of_range():
    payload = valid_payload()
    payload["questions"][0]["answer_index"] = 9
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)
    assert info.value.code == "invalid_answer_index"


def test_handout_completeness():
    payload = valid_payload()
    payload["handout"]["pitfalls"] = []
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)
    assert info.value.code == "handout_incomplete"


def test_duplicate_stem():
    payload = valid_payload()
    payload["questions"][1]["stem"] = payload["questions"][0]["stem"]
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)
    assert info.value.code == "duplicate_stem"


def test_insufficient_evidence_is_separate_from_failure():
    payload = {
        "progress_reading": {},
        "handout": {},
        "questions": [],
        "insufficient_evidence": {
            "supportable_question_count": 1,
            "reason": "资料不足",
            "suggested_pages": "12-16",
        },
    }
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)
    assert info.value.code == "insufficient_evidence"

def test_image_question_with_only_text_citation_is_rejected():
    payload = valid_payload()
    payload["questions"][0]["is_image_based"] = True
    payload["questions"][0]["citations"] = [{"ref": "T1", "claim": "只有文字证据"}]
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)
    assert info.value.code == "missing_image_citation"


def test_image_question_with_image_citation_is_accepted():
    payload = valid_payload()
    payload["questions"][0]["is_image_based"] = True
    payload["questions"][0]["citations"] = [{"ref": "F1", "claim": "图像证据"}]
    validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)

def test_route_table_mentioned_as_exclusion_is_not_out_of_scope():
    payload = valid_payload()
    payload["questions"][0]["stem"] = "交换机转发数据帧时是否需要查路由表？"
    validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)


def test_route_table_calculation_is_still_out_of_scope():
    payload = valid_payload()
    payload["questions"][0]["stem"] = "请计算路由表中的下一跳地址"
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)
    assert info.value.code == "out_of_scope"
    assert "路由表" in info.value.message


def test_route_table_discussed_by_evidence_is_not_out_of_scope():
    payload = valid_payload()
    payload["questions"][0]["stem"] = "路由器依据路由表转发分组时，先查什么？"
    evidence = RefMap(
        {
            "F1": RefTarget(
                evidence_id="ev_4",
                source_id="src_1",
                kind="image_region",
                page_no=None,
                confidence="high",
                locator_json="{}",
                quote_text="拓扑图",
            ),
            "T1": RefTarget(
                evidence_id="ev_3",
                source_id="src_1",
                kind="text_span",
                page_no=2,
                confidence="high",
                locator_json="{}",
                quote_text="路由器根据路由表转发分组",
            ),
        }
    )
    validate_output(ModelOutput.model_validate(payload), 3, evidence, True)


def test_unlearned_protocol_name_is_still_out_of_scope():
    payload = valid_payload()
    payload["questions"][0]["stem"] = "OSPF 与 RIP 的区别是什么？"
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)
    assert info.value.code == "out_of_scope"
    assert "OSPF" in info.value.message


def test_declared_unlearned_term_is_still_out_of_scope():
    payload = valid_payload()
    payload["progress_reading"] = {"unlearned": ["路由表"]}
    payload["questions"][0]["stem"] = "交换机转发数据帧时是否需要查路由表？"
    with pytest.raises(ValidationError) as info:
        validate_output(ModelOutput.model_validate(payload), 3, ref_map(), True)
    assert info.value.code == "out_of_scope"
    assert "未学内容" in info.value.message