from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA_DIR = Path(tempfile.mkdtemp(prefix="xunzhi-stage2-demo-"))
os.environ["XUNZHI_DATA_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ALLOW_FAKE"] = "1"
os.environ["XUNZHI_FAKE_MODE"] = "ok"
os.environ["DEEPSEEK_API_KEY"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.domain.models import KnowledgePoint, Question, QuestionKnowledgePoint  # noqa: E402
from app.infra.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from tests.stage4_helpers import PROGRESS, make_png, upload, wait_job  # noqa: E402


def points_for(pack: dict) -> dict[str, dict]:
    ids = [item["id"] for item in pack["questions"]]
    with SessionLocal() as db:
        links = db.execute(
            __import__("sqlalchemy").select(QuestionKnowledgePoint).where(
                QuestionKnowledgePoint.question_id.in_(ids)
            )
        ).scalars().all()
        return {
            link.question_id: {
                "id": link.knowledge_point_id,
                "name": db.get(KnowledgePoint, link.knowledge_point_id).display_name,
            }
            for link in links
        }


def create_plan(client, source_id, points, count, goal):
    body = {
        "title": "演示计划",
        "sources": [{"source_id": source_id}],
        "goal": goal,
        "question_count": count,
        "points": [
            {
                "knowledge_point_id": item["id"],
                "display_name": item["name"],
                "suggested_question_count": count // len(points),
            }
            for item in points
        ],
    }
    plan = client.post("/api/plans", json=body).json()
    version = plan["current_version_id"]
    assert client.post(f"/api/plans/{plan['id']}/versions/{version}/confirm").status_code == 200
    return version


def create_pack(client, source_id, version_id, count):
    body = {
        "idempotency_key": f"demo-{time.time_ns()}",
        "sources": [{"source_id": source_id}],
        "progress_text": PROGRESS,
        "difficulty": "consolidate",
        "question_count": count,
        "consent_ack": True,
        "plan_version_id": version_id,
    }
    created = client.post("/api/packs", json=body)
    assert created.status_code == 202, created.text
    job = wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job
    return client.get(f"/api/packs/{job['pack_id']}").json()


def submit(client, pack, modes):
    answers = []
    for question in pack["questions"]:
        mode = modes.get(question["id"])
        if mode is None:
            continue
        with SessionLocal() as db:
            row = db.get(Question, question["id"])
            index = row.answer_index if mode == "correct" else (row.answer_index + 1) % 4
        answers.append({"question_id": question["id"], "selected_index": index})
    return client.post(
        f"/api/packs/{pack['id']}/submit",
        json={"idempotency_key": f"demo-submit-{time.time_ns()}", "answers": answers},
    ).json()


def state(client, point_id):
    data = client.get(f"/api/knowledge-points/{point_id}/mastery").json()["mastery"]
    return data["state"], data["flags"]


def allocation(version):
    return {
        item["display_name"]: item["suggested_question_count"]
        for item in version["items"]
    }


with TestClient(app) as client:
    source = upload(client, make_png(Path(tempfile.mkdtemp(dir=DATA_DIR)) / "demo.png", (12, 34, 56)))
    _, base = (None, client.get(f"/api/packs/{wait_job(client, client.post('/api/packs', json={'idempotency_key': f'base-{time.time_ns()}', 'sources': [{'source_id': source['id']}], 'progress_text': PROGRESS, 'difficulty': 'consolidate', 'question_count': 3, 'consent_ack': True}).json()['job_id'])['pack_id']}").json())
    base_points = points_for(base)
    points = list(base_points.values())
    print("[A0] 初始状态:", {item["name"]: state(client, item["id"])[0] for item in points})

    v1 = create_plan(client, source["id"], points, 3, "先复习三个考点")
    pack1 = create_pack(client, source["id"], v1, 3)
    v1_points = points_for(pack1)
    q1, q2, q3 = pack1["questions"]
    p1, p2, p3 = v1_points[q1["id"]], v1_points[q2["id"]], v1_points[q3["id"]]
    submitted = submit(client, pack1, {q1["id"]: "wrong", q2["id"]: "correct"})
    print("[A2] 交卷后:", {
        p1["name"]: state(client, p1["id"])[0],
        p2["name"]: state(client, p2["id"])[0],
        p3["name"]: state(client, p3["id"])[0],
    })

    suggested = client.post(
        f"/api/sources/{source['id']}/next-plan",
        json={
            "goal": "优先补同网段判断",
            "question_count": 5,
            "points": [{"knowledge_point_id": item["id"], "display_name": item["name"]} for item in points],
        },
    ).json()
    print("[A3] 下次建议 v2:", allocation(suggested["version"]))
    for item in suggested["version"]["adjustment"]:
        print(
            f"     {item['display_name']}: 上次 {item['previous_question_count']} 道 -> 本次 {item['current_question_count']} 道；{item['reason']}"
        )
    v2 = suggested["version"]["id"]
    assert client.post(f"/api/plans/{suggested['plan']['id']}/versions/{v2}/confirm").status_code == 200
    pack2 = create_pack(client, source["id"], v2, 5)
    print(
        "[A4] 实际生成:",
        pack2["plan"]["version_id"],
        "计划/实际=",
        pack2["plan"]["delivery"]["expected"]["total"],
        "/",
        pack2["plan"]["delivery"]["actual"]["total"],
        "shortfall=",
        pack2["plan"]["delivery"]["shortfall"],
    )

    client.post(f"/api/packs/{pack1['id']}/reattempt")
    submit(client, pack1, {q1["id"]: "correct"})
    print("[A5] 原题订正后:", state(client, p1["id"]))

    v2_points = points_for(pack2)
    new_question = next(
        item["id"]
        for item in pack2["questions"]
        if v2_points[item["id"]]["id"] == p1["id"] and item["ordinal"] >= 4
    )
    submit(client, pack2, {new_question: "correct"})
    print("[A6] 新题验证后:", state(client, p1["id"]))
    stable_plan = client.post(
        f"/api/sources/{source['id']}/next-plan",
        json={
            "goal": "稳定后降低密度",
            "question_count": 3,
            "points": [{"knowledge_point_id": item["id"], "display_name": item["name"]} for item in points],
        },
    ).json()
    print("[A6b] 稳定后建议 3 题:", allocation(stable_plan["version"]))

    client.post(f"/api/packs/{pack2['id']}/reattempt")
    submit(client, pack2, {new_question: "wrong"})
    print("[A7] 再次答错后:", state(client, p1["id"]))

    next_again = client.post(
        f"/api/sources/{source['id']}/next-plan",
        json={
            "goal": "按最新状态再安排",
            "question_count": 3,
            "points": [{"knowledge_point_id": item["id"], "display_name": item["name"]} for item in points],
        },
    ).json()
    print("[A8] 再建议 3 题:", allocation(next_again["version"]))

shutil.rmtree(DATA_DIR, ignore_errors=True)