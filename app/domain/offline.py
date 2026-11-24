from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT
from app.domain.grading import grade
from app.domain.models import OfflineState, now_iso

SEED_PATH = PROJECT_ROOT / "seeds" / "offline_sample.json"


class OfflineError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def load_seed() -> dict:
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    if seed.get("version") != "1":
        raise OfflineError("seed_invalid", "离线示例版本不匹配", 500)
    if not seed.get("questions") or not seed.get("handout"):
        raise OfflineError("seed_invalid", "离线示例数据不完整", 500)
    return seed


def get_state(db: Session) -> OfflineState:
    state = db.get(OfflineState, 1)
    if state is None:
        state = OfflineState(id=1, sample_version="1", round_no=1, submitted=False, completed=False, updated_at=now_iso())
        db.add(state)
        db.commit()
    return state


def _citation(ref: str, seed: dict) -> dict:
    return {
        "id": f"offline_cit_{ref}",
        "kind": "image_region",
        "label": "原创拓扑图 · 区域 1 / 1",
        "claim": "离线示例引用",
        "source_name": "原创示例拓扑图",
        "source_status": "offline",
        "page_no": None,
        "page_count": None,
        "boxes": [[0.0, 0.0, 1.0, 1.0]],
        "quote": seed.get("topology_alt", ""),
        "text_alternative": seed.get("topology_alt", ""),
        "asset_url": seed.get("topology_url"),
        "is_offline": True,
    }


def _public_question(question: dict, seed: dict) -> dict:
    return {
        "id": question["id"],
        "ordinal": question["ordinal"],
        "stem": question["stem"],
        "options": [{"key": chr(65 + i), "text": text} for i, text in enumerate(question["options"])],
        "is_image_based": bool(question.get("is_image_based")),
        "citations": [_citation("F1", seed)],
    }


def _result_payload(seed: dict, state: OfflineState, questions: list[dict]) -> dict:
    answers = json.loads(state.answers_json) if state.answers_json else {}
    rows = [{"id": item["id"], "answer_index": item["answer_index"]} for item in questions]
    graded = grade(
        rows,
        [{"question_id": qid, "selected_index": idx} for qid, idx in answers.items()],
    )
    by_id = {item["id"]: item for item in questions}
    focus = json.loads(state.focus_json) if state.focus_json else []
    return {
        "is_offline": True,
        "score": {
            "correct": graded.correct,
            "wrong": graded.wrong,
            "unanswered": graded.unanswered,
            "total": graded.total,
            "accuracy": graded.accuracy,
        },
        "questions": [
            {
                "question_id": item.question_id,
                "ordinal": by_id[item.question_id]["ordinal"],
                "stem": by_id[item.question_id]["stem"],
                "options": [
                    {"key": chr(65 + i), "text": text}
                    for i, text in enumerate(by_id[item.question_id]["options"])
                ],
                "selected_index": item.selected_index,
                "answer_index": item.answer_index,
                "is_correct": item.is_correct,
                "explanation": by_id[item.question_id]["explanation"],
                "knowledge_point": by_id[item.question_id]["knowledge_point"],
                "citations": [_citation("F1", seed)],
            }
            for item in graded.answers
        ],
        "wrong_knowledge_points": focus,
        "completed": state.completed,
        "completed_text": seed.get("completed_text", "") if state.completed else "",
        "next_round_suggestion": {
            "available": bool(focus and not state.completed),
            "reason": "本轮存在错题，可按考点再练" if focus and not state.completed else "本轮无错题",
        },
    }


def sample_payload(db: Session) -> dict:
    seed = load_seed()
    state = get_state(db)
    questions = seed["questions"] if state.round_no == 1 else _round_questions(seed, state)
    result = _result_payload(seed, state, questions) if state.submitted else None
    return {
        "is_offline": True,
        "version": seed["version"],
        "title": seed["title"],
        "topology_url": seed.get("topology_url"),
        "handout": _handout_payload(seed),
        "questions": [_public_question(item, seed) for item in questions],
        "submitted": state.submitted,
        "completed": state.completed,
        "round_no": state.round_no,
        "completed_text": seed.get("completed_text", ""),
        "answers": json.loads(state.answers_json) if state.answers_json else {},
        "result": result,
    }


def _handout_payload(seed: dict) -> list[dict]:
    handout = seed["handout"]
    result = [
        {
            "id": "offline_scope",
            "section_type": "scope",
            "heading": "学习范围",
            "body": "本次覆盖：" + "；".join(handout["scope"]["covered"]) + "\n本次不覆盖：" + "；".join(handout["scope"]["not_covered"]),
            "content": handout["scope"],
            "citations": [],
            "is_extension": False,
        }
    ]
    mapping = [("key_points", "key_point"), ("pitfalls", "pitfall"), ("visual", "visual")]
    for key, section_type in mapping:
        for index, block in enumerate(handout.get(key, []), start=1):
            result.append(
                {
                    "id": f"offline_{section_type}_{index}",
                    "section_type": section_type,
                    "heading": block.get("heading"),
                    "body": block["body"],
                    "content": None,
                    "citations": [_citation("F1", seed)],
                    "is_extension": False,
                }
            )
    return result


def _round_questions(seed: dict, state: OfflineState) -> list[dict]:
    focus = json.loads(state.focus_json) if state.focus_json else []
    pool = seed.get("retry_pool", [])
    selected = [item for item in pool if item["knowledge_point"] in focus]
    if not selected:
        selected = pool
    return selected


def submit(db: Session, answers: list[dict]) -> dict:
    seed = load_seed()
    state = get_state(db)
    if state.submitted:
        raise OfflineError("already_submitted", "本轮示例已提交，可重置后再练")
    questions = seed["questions"] if state.round_no == 1 else _round_questions(seed, state)
    rows = [{"id": item["id"], "answer_index": item["answer_index"]} for item in questions]
    graded = grade(
        rows,
        [{"question_id": item["question_id"], "selected_index": item.get("selected_index")} for item in answers],
    )
    state.submitted = True
    state.answers_json = json.dumps(
        {item.question_id: item.selected_index for item in graded.answers}, ensure_ascii=False
    )
    state.completed = graded.correct == graded.total and graded.total > 0
    state.updated_at = now_iso()
    if state.completed:
        state.focus_json = json.dumps([], ensure_ascii=False)
    else:
        wrong = [
            questions[index]["knowledge_point"]
            for index, item in enumerate(graded.answers)
            if item.is_correct is False
        ]
        state.focus_json = json.dumps(sorted(set(wrong)), ensure_ascii=False)
    db.commit()
    return {"duplicate": False, **_result_payload(seed, state, questions)}


def retry(db: Session, focus_ids: list[str] | None = None) -> dict:
    seed = load_seed()
    state = get_state(db)
    if not state.submitted:
        raise OfflineError("not_submitted", "请先提交本轮示例作答")
    allowed = json.loads(state.focus_json) if state.focus_json else []
    if focus_ids:
        focuses = [item for item in focus_ids if item in allowed]
        if not focuses:
            raise OfflineError(
                "focus_mismatch", "所选复习重点不属于本轮错题，请刷新后按当前轮次再练", 422
            )
    else:
        focuses = allowed
    if not focuses:
        raise OfflineError("no_retry_questions", "本轮无错题，无需再练", 422)
    pool = seed.get("retry_pool", [])
    selected = [item for item in pool if item["knowledge_point"] in focuses]
    if not selected:
        raise OfflineError("no_retry_questions", "按这些考点没有可用的示例题", 422)
    state.round_no += 1
    state.submitted = False
    state.completed = False
    state.answers_json = None
    state.focus_json = json.dumps(focuses, ensure_ascii=False)
    state.updated_at = now_iso()
    db.commit()
    return {
        "is_offline": True,
        "round_no": state.round_no,
        "questions": [_public_question(item, seed) for item in selected],
        "focus": focuses,
    }


def reset(db: Session) -> dict:
    state = get_state(db)
    state.sample_version = "1"
    state.round_no = 1
    state.submitted = False
    state.completed = False
    state.answers_json = None
    state.focus_json = None
    state.diagnosis_json = None
    state.updated_at = now_iso()
    db.commit()
    return {"is_offline": True, "reset": True, "round_no": 1}


DIAGNOSIS_BRANCHES = {
    "a": {
        "branch": "a",
        "label": "已学过子网掩码和跨网段判断",
        "question": "根据 IP 与掩码判断是否同网段时，先看哪一部分？",
        "options": [
            "IP 与掩码按位与后的网络地址",
            "只看设备数量",
            "只看默认网关",
        ],
        "plan": {
            "total": 3,
            "items": [
                {"display_name": "判断同网段", "count": 2},
                {"display_name": "识别交换机端口", "count": 1},
            ],
            "excluded": [],
        },
    },
    "b": {
        "branch": "b",
        "label": "只认识交换机和路由器，还没学子网",
        "question": "交换机和路由器的主要区别是什么？",
        "options": [
            "交换机在同一网段按 MAC 转发，路由器连接不同网段",
            "两者完全一样",
            "路由器只连接终端设备",
        ],
        "plan": {
            "total": 3,
            "items": [
                {"display_name": "识别交换机端口", "count": 2},
                {"display_name": "选择默认网关", "count": 1},
            ],
            "excluded": ["判断同网段"],
        },
    },
}


def diagnosis_sample(db: Session) -> dict:
    state = get_state(db)
    return {
        "is_offline": True,
        "is_offline_sample": True,
        "step": "scope",
        "question": {
            "id": "offline_dx_1",
            "stem": "这份示例资料里，你目前更接近哪种情况？",
            "options": [
                {"key": "A", "text": DIAGNOSIS_BRANCHES["a"]["label"]},
                {"key": "B", "text": DIAGNOSIS_BRANCHES["b"]["label"]},
            ],
        },
        "saved": json.loads(state.diagnosis_json) if state.diagnosis_json else None,
    }


def diagnosis_answer(db: Session, branch: str) -> dict:
    if branch not in DIAGNOSIS_BRANCHES:
        raise OfflineError("branch_invalid", "离线诊断分支不存在", 422)
    state = get_state(db)
    payload = DIAGNOSIS_BRANCHES[branch]
    state.diagnosis_json = json.dumps(payload, ensure_ascii=False)
    state.updated_at = now_iso()
    db.commit()
    return {
        "is_offline": True,
        "is_offline_sample": True,
        "branch": branch,
        "question": {
            "id": f"offline_dx_{branch}_2",
            "stem": payload["question"],
            "options": [{"key": chr(65 + i), "text": text} for i, text in enumerate(payload["options"])],
        },
        "plan": payload["plan"],
    }


def diagnosis_reset(db: Session) -> dict:
    state = get_state(db)
    state.diagnosis_json = None
    state.updated_at = now_iso()
    db.commit()
    return {"is_offline": True, "reset": True}