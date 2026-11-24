from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class GradedAnswer:
    question_id: str
    selected_index: int | None
    answer_index: int
    is_correct: bool | None


@dataclass
class GradeResult:
    correct: int
    wrong: int
    unanswered: int
    total: int
    accuracy: float
    answers: list[GradedAnswer]

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["answers"] = [asdict(item) for item in self.answers]
        return payload


def grade(questions: list[dict], answers: list[dict]) -> GradeResult:
    """questions: [{id, answer_index}], answers: [{question_id, selected_index}]"""
    by_id = {item["id"]: item for item in questions}
    chosen = {item["question_id"]: item.get("selected_index") for item in answers}
    graded: list[GradedAnswer] = []
    for question in questions:
        selected = chosen.get(question["id"])
        if selected is None:
            graded.append(GradedAnswer(question["id"], None, question["answer_index"], None))
        else:
            graded.append(
                GradedAnswer(
                    question["id"],
                    selected,
                    question["answer_index"],
                    selected == question["answer_index"],
                )
            )
    correct = sum(1 for item in graded if item.is_correct is True)
    unanswered = sum(1 for item in graded if item.selected_index is None)
    wrong = len(graded) - correct - unanswered
    total = len(graded)
    accuracy = round(correct / total, 4) if total else 0.0
    return GradeResult(correct, wrong, unanswered, total, accuracy, graded)


def four_state_total(questions: list[dict], answers: list[dict]) -> dict:
    result = grade(questions, answers)
    return {
        "answered": result.total - result.unanswered,
        "unanswered": result.unanswered,
        "total": result.total,
    }