from __future__ import annotations

import json
import re
from pathlib import Path

from app.domain.schemas import ModelOutput, RefMap, RefTarget
from app.domain.validation import validate_output

ROOT = Path(__file__).resolve().parents[2]
PROMPT = ROOT / "app" / "prompts" / "system_generate.md"
SCHEMA = ROOT / "app" / "prompts" / "output_schema.json"


def prompt_text() -> str:
    return PROMPT.read_text(encoding="utf-8")


def schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def prompt_example() -> dict:
    text = prompt_text()
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    assert match, "prompt must contain one JSON example"
    return json.loads(match.group(1))


def ref_map() -> RefMap:
    return RefMap(
        {
            "T1": RefTarget(
                evidence_id="ev_prompt_t1",
                source_id="src_prompt_t1",
                kind="text_span",
                page_no=1,
                confidence="high",
                locator_json="{}",
                quote_text="文本证据",
            ),
            "F1": RefTarget(
                evidence_id="ev_prompt_f1",
                source_id="src_prompt_f1",
                kind="image_region",
                page_no=None,
                confidence="high",
                locator_json="{}",
                quote_text="图像证据",
            ),
        }
    )


def test_prompt_states_validation_limits_and_evidence_boundary():
    text = prompt_text()
    for token in (
        "3–6",
        "2–4",
        "covered",
        "not_covered",
        "visual",
        "题号",
        "任务给定值",
        "不要照抄",
        "insufficient_evidence",
        "supportable_question_count",
        "题干",
        "不得重复",
        "至少一半",
        "零基础",
        "设备作用",
        "读图题",
        "图像证据",
        "未学机制",
        "逐题核对",
        "混合结论",
        "帧",
        "本次",
    ):
        assert token in text, token
    assert "至少 1 条引用" in text
    assert "不得只写其中一侧" in text


def test_prompt_example_has_required_minimum_structure():
    example = prompt_example()
    handout = example["handout"]
    assert len(handout["scope"]["covered"]) >= 1
    assert len(handout["scope"]["not_covered"]) >= 1
    assert len(handout["key_points"]) >= 3
    assert len(handout["pitfalls"]) >= 2
    assert len(handout["visual"]) >= 1

    questions = example["questions"]
    assert len(questions) >= 3
    stems = [item["stem"] for item in questions]
    assert len(set(stems)) == len(stems), "prompt example question stems must be distinct"
    assert all(stem and stem != "..." for stem in stems)
    assert [item["ordinal"] for item in questions] == list(range(1, len(questions) + 1))
    for item in questions:
        assert len(item["options"]) == 4
        assert len(set(item["options"])) == 4
        assert 0 <= item["answer_index"] <= 3
        assert item["explanation"]
        assert item["citations"]
    assert "T1" in json.dumps(example, ensure_ascii=False)


def test_prompt_example_passes_existing_validation():
    example = prompt_example()
    output = ModelOutput.model_validate(example)
    validate_output(output, expected_count=len(example["questions"]), ref_map=ref_map(), has_image=True)


def test_schema_aligns_with_validation_and_prompt():
    data = schema()
    assert len(data["oneOf"]) == 2

    success = data["$defs"]["success_response"]
    assert success["required"] == ["progress_reading", "handout", "questions"]
    assert success["properties"]["insufficient_evidence"]["type"] == "null"

    handout = data["$defs"]["handout"]["properties"]
    assert data["$defs"]["handout"]["required"] == ["scope", "key_points", "pitfalls"]
    assert handout["scope"]["required"] == ["covered", "not_covered"]
    assert handout["scope"]["properties"]["covered"]["minItems"] == 1
    assert handout["scope"]["properties"]["not_covered"]["minItems"] == 1
    assert handout["key_points"]["minItems"] == 3
    assert handout["key_points"]["maxItems"] == 6
    assert handout["pitfalls"]["minItems"] == 2
    assert handout["pitfalls"]["maxItems"] == 4
    assert "visual" in handout

    questions = data["$defs"]["questions"]
    assert questions["minItems"] == 3
    assert questions["maxItems"] == 8
    item = data["$defs"]["question"]["properties"]
    assert item["options"]["minItems"] == 4
    assert item["options"]["maxItems"] == 4
    assert item["citations"]["minItems"] == 1
    assert item["answer_index"]["minimum"] == 0
    assert item["answer_index"]["maximum"] == 3

    citation = data["$defs"]["citation"]["properties"]["ref"]
    assert citation["pattern"] == "^(T|F|P|I)[1-9][0-9]*$"


def test_insufficient_response_can_be_minimal_and_still_requires_count():
    data = schema()
    branch = data["$defs"]["insufficient_response"]
    assert branch["required"] == ["insufficient_evidence"]
    assert branch["additionalProperties"] is False
    payload = branch["properties"]["insufficient_evidence"]
    assert payload["type"] == "object"
    assert payload["required"] == ["supportable_question_count"]
    assert payload["properties"]["supportable_question_count"]["type"] == "integer"

    minimal = {"insufficient_evidence": {"supportable_question_count": 1}}
    assert set(minimal) == set(branch["required"])
    assert set(minimal["insufficient_evidence"]) <= set(payload["properties"])
    assert "supportable_question_count" in payload["required"]

def test_prompt_scope_ceiling_and_device_identification_example():
    text = prompt_text()
    for token in (
        "只想",
        "只学",
        "仅复习",
        "范围上限",
        "范围声明",
        "知识要点",
        "易错处",
        "图文解释",
        "题干",
        "所有选项",
        "解析",
        "设备识别",
        "同/跨网段",
        "默认网关",
        "MAC",
        "ARP",
        "转发步骤",
        "自查",
        "证据不足",
        "不是关键词黑名单",
    ):
        assert token in text, token
    assert "目标即范围上限" in text
    assert "B：刚开始学习" in text
    assert "不要照抄固定样例" in text