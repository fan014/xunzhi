from __future__ import annotations

from app.domain.mastery import (
    AttemptView,
    MASTERY_RULE_VERSION,
    classify_attempts,
    question_reuse,
)


def _row(question_id: str, result: str, *, ordinal: int = 1, origin=None, stem=None) -> dict:
    return {
        "question_id": question_id,
        "result": result,
        "ordinal": ordinal,
        "origin_question_id": origin,
        "stem_hash": stem,
        "reliability": "confirmed",
        "source_type": "practice",
    }


def _view(attempt_id: str, submitted_at: str | None, rows: list[dict]) -> AttemptView:
    return AttemptView(attempt_id=attempt_id, submitted_at=submitted_at, rows=rows)


def test_no_evidence_is_pending():
    result = classify_attempts([], [])
    assert result.state == "pending"
    assert result.state_label == "待评估"
    assert result.rule_version == MASTERY_RULE_VERSION


def test_single_wrong_is_needs_reinforce():
    result = classify_attempts([_view("a1", "2026-11-01T00:00:00Z", [_row("q1", "wrong")])], [])
    assert result.state == "needs_reinforce"
    assert "答错" in result.decision_reason


def test_single_new_correct_is_consolidating():
    result = classify_attempts([_view("a1", "2026-11-01T00:00:00Z", [_row("q1", "correct")])], [])
    assert result.state == "consolidating"
    assert "single_attempt" in result.flags


def test_original_redo_is_consolidating_with_flag():
    earlier = _view("a0", "2026-10-30T00:00:00Z", [_row("q1", "wrong")])
    latest = _view("a1", "2026-11-01T00:00:00Z", [_row("q1", "correct")])
    result = classify_attempts([latest, earlier], [])
    assert result.state == "consolidating"
    assert "corrected_pending_new" in result.flags


def test_two_all_correct_with_new_question_is_stable():
    older = _view("a1", "2026-10-30T00:00:00Z", [_row("q1", "correct")])
    latest = _view("a2", "2026-11-01T00:00:00Z", [_row("q2", "correct")])
    result = classify_attempts([latest, older], [])
    assert result.state == "stable"
    assert result.new_question_since_previous is True


def test_two_all_correct_same_question_is_not_stable():
    older = _view("a1", "2026-10-30T00:00:00Z", [_row("q1", "correct")])
    latest = _view("a2", "2026-11-01T00:00:00Z", [_row("q1", "correct")])
    result = classify_attempts([latest, older], [])
    assert result.state == "consolidating"
    assert "corrected_pending_new" in result.flags


def test_one_attempt_many_correct_is_not_stable():
    rows = [_row(f"q{i}", "correct", ordinal=i) for i in range(3)]
    result = classify_attempts([_view("a1", "2026-11-01T00:00:00Z", rows)], [])
    assert result.state == "consolidating"


def test_latest_wrong_returns_to_needs_reinforce():
    older = _view("a1", "2026-10-30T00:00:00Z", [_row("q1", "correct")])
    latest = _view("a2", "2026-11-01T00:00:00Z", [_row("q2", "wrong", ordinal=2)])
    result = classify_attempts([latest, older], [])
    assert result.state == "needs_reinforce"


def test_unanswered_only_is_pending():
    result = classify_attempts([], [_view("a1", "2026-11-01T00:00:00Z", [_row("q1", "unanswered")])])
    assert result.state == "pending"
    assert result.unanswered_only is True


def test_unknown_origin_is_not_new_evidence():
    earlier = _view("a1", "2026-10-30T00:00:00Z", [_row("q1", "correct")])
    latest = _view("a2", "2026-11-01T00:00:00Z", [_row("q2", "correct", origin="q_missing")])
    result = classify_attempts([latest, earlier], [])
    assert result.state == "consolidating"


def test_question_reuse_order():
    assert question_reuse("q1", {"q1"}) == "original_redo"
    assert question_reuse("q2", {"q1"}, origin_question_id="q1") == "original_redo"
    assert question_reuse("q2", {"q1"}, origin_question_id="missing") == "unknown"
    assert question_reuse("q2", {"q1"}, stem_hash="h1", earlier_hashes={"h1"}) == "original_redo"
    assert question_reuse("q2", {"q1"}, stem_hash="h2", earlier_hashes={"h1"}) == "new_question"


def test_rule_is_reproducible():
    view = _view("a1", "2026-11-01T00:00:00Z", [_row("q1", "wrong")])
    first = classify_attempts([view], []).to_dict()
    second = classify_attempts([view], []).to_dict()
    assert first == second