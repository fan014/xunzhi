from __future__ import annotations

from app.domain import offline
from app.infra.db import SessionLocal


def test_sample_is_unanswered_and_has_no_answers():
    with SessionLocal() as db:
        offline.reset(db)
        payload = offline.sample_payload(db)
    assert payload["is_offline"] is True
    assert payload["submitted"] is False
    assert len(payload["questions"]) == 5
    for question in payload["questions"]:
        assert len(question["options"]) == 4
        assert "answer_index" not in question
        assert "explanation" not in question


def test_submit_all_correct_completes_sample():
    with SessionLocal() as db:
        offline.reset(db)
        payload = offline.sample_payload(db)
        correct = []
        for question in payload["questions"]:
            seed_question = next(
                item for item in offline.load_seed()["questions"] if item["id"] == question["id"]
            )
            correct.append(
                {"question_id": question["id"], "selected_index": seed_question["answer_index"]}
            )
        result = offline.submit(db, correct)
    assert result["is_offline"] is True
    assert result["completed"] is True
    assert result["score"]["correct"] == 5
    assert result["next_round_suggestion"]["available"] is False
    assert result["completed_text"]


def test_wrong_answer_creates_focus_and_retry_round():
    with SessionLocal() as db:
        offline.reset(db)
        payload = offline.sample_payload(db)
        answers = []
        for question in payload["questions"]:
            seed_question = next(
                item for item in offline.load_seed()["questions"] if item["id"] == question["id"]
            )
            selected = (seed_question["answer_index"] + 1) % 4
            answers.append({"question_id": question["id"], "selected_index": selected})
        result = offline.submit(db, answers)
        assert result["score"]["correct"] == 0
        assert result["wrong_knowledge_points"]
        retry = offline.retry(db, result["wrong_knowledge_points"])
        assert retry["is_offline"] is True
        assert retry["round_no"] == 2
        assert retry["questions"]
    with SessionLocal() as db:
        offline.reset(db)