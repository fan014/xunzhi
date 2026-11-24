from __future__ import annotations

import json

import pytest

from app.domain.diagnosis import DiagnosisParseError, _parse_json_payload


def _scope(stem: str = "你学过这个考点吗？") -> dict:
    return {
        "question_type": "scope",
        "stem": stem,
        "options": ["学过", "没学过"],
        "citation_ref": "T1",
    }


def test_single_object():
    parsed = _parse_json_payload(json.dumps(_scope(), ensure_ascii=False))
    assert parsed.object_count == 1
    assert parsed.data["stem"] == "你学过这个考点吗？"
    assert parsed.used_fence is False


def test_complete_code_fence():
    text = "```json\n" + json.dumps(_scope(), ensure_ascii=False) + "\n```"
    parsed = _parse_json_payload(text)
    assert parsed.object_count == 1
    assert parsed.used_fence is True
    assert parsed.data["question_type"] == "scope"


def test_braces_and_escaped_quotes_in_stem_are_valid_json():
    stem = '判断 {PC-A} 是否在 "192.168.1.0/24" 内？'
    text = json.dumps(_scope(stem), ensure_ascii=False)
    parsed = _parse_json_payload(text)
    assert parsed.data["stem"] == stem
    assert parsed.object_count == 1


def test_two_objects_take_the_first():
    first = _scope("第一道题")
    second = _scope("第二道题")
    text = json.dumps(first, ensure_ascii=False) + "\n" + json.dumps(second, ensure_ascii=False)
    parsed = _parse_json_payload(text)
    assert parsed.object_count == 2
    assert parsed.data["stem"] == "第一道题"


def test_fenced_two_objects_take_the_first():
    first = _scope("围栏第一道题")
    second = _scope("围栏第二道题")
    text = (
        "```json\n"
        + json.dumps(first, ensure_ascii=False)
        + "\n"
        + json.dumps(second, ensure_ascii=False)
        + "\n```"
    )
    parsed = _parse_json_payload(text)
    assert parsed.object_count == 2
    assert parsed.data["stem"] == "围栏第一道题"
    assert parsed.used_fence is True


def test_truncated_second_object_rejects_whole_response():
    first = json.dumps(_scope("第一道完整"), ensure_ascii=False)
    text = first + "\n" + '{"question_type": "probe_question", "stem": "第二道残缺'
    with pytest.raises(DiagnosisParseError) as info:
        _parse_json_payload(text)
    assert info.value.object_count == 1
    assert info.value.position is not None
    assert info.value.position > 0
    assert info.value.reason == "invalid_json"


def test_array_is_rejected():
    with pytest.raises(DiagnosisParseError) as info:
        _parse_json_payload(json.dumps([_scope(), _scope()], ensure_ascii=False))
    assert info.value.reason == "not_object"
    assert info.value.object_count == 0


def test_prose_prefix_is_not_cut_with_braces():
    text = '这是本轮题目：{"question_type": "scope", "stem": "带前缀", "options": ["a", "b"], "citation_ref": "T1"}'
    with pytest.raises(DiagnosisParseError) as info:
        _parse_json_payload(text)
    assert info.value.object_count == 0
    assert info.value.position is not None


def test_trailing_garbage_is_rejected():
    text = json.dumps(_scope("第一道"), ensure_ascii=False) + "\n这里是第二道题的说明"
    with pytest.raises(DiagnosisParseError) as info:
        _parse_json_payload(text)
    assert info.value.object_count == 1
    assert info.value.reason == "invalid_json"


def test_empty_or_whitespace_is_rejected():
    for text in ("", "   \n\t  "):
        with pytest.raises(DiagnosisParseError) as info:
            _parse_json_payload(text)
        assert info.value.object_count == 0