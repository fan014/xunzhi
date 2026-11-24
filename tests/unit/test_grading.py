from __future__ import annotations

from app.domain.grading import four_state_total, grade


QUESTIONS = [
    {"id": "q1", "answer_index": 0},
    {"id": "q2", "answer_index": 1},
    {"id": "q3", "answer_index": 2},
    {"id": "q4", "answer_index": 3},
    {"id": "q5", "answer_index": 0},
]


def test_partial_answers_are_counted_separately():
    result = grade(
        QUESTIONS,
        [
            {"question_id": "q1", "selected_index": 0},
            {"question_id": "q2", "selected_index": 2},
            {"question_id": "q3", "selected_index": None},
        ],
    )
    assert (result.correct, result.wrong, result.unanswered, result.total) == (1, 1, 3, 5)
    assert result.accuracy == 0.2
    unanswered = [item for item in result.answers if item.selected_index is None]
    assert len(unanswered) == 3
    assert all(item.is_correct is None for item in unanswered)


def test_all_unanswered_is_not_wrong():
    result = grade(QUESTIONS, [])
    assert result.wrong == 0
    assert result.unanswered == 5


def test_four_state_total():
    stats = four_state_total(QUESTIONS, [{"question_id": "q1", "selected_index": 0}])
    assert stats == {"answered": 1, "unanswered": 4, "total": 5}