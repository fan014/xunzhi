from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx

BASE = os.environ.get("RESTART_BASE", "http://127.0.0.1:8011")
DB = Path(os.environ.get("RESTART_DB", "data_restart/xunzhi.sqlite3"))
STATE = Path(os.environ.get("RESTART_STATE", "restart_state.json"))
TEXT_DIR = Path("acceptance_texts")
GOAL = "学习三个主题，重点掌握 TCP 快速重传触发条件。"

client = httpx.Client(base_url=BASE, timeout=120.0)


def api(method: str, path: str, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise SystemExit(f"{method} {path} -> {response.status_code} {response.text[:400]}")
    return response.json()


def _text() -> str:
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    path = TEXT_DIR / "three_topics.txt"
    if not path.exists():
        prefix = (
            "第一部分：交换机根据目的 MAC 地址在同网段内转发帧，主机在同一广播域内直接通信。"
            "判断时应先看地址范围与默认网关，不要把跨设备误认为跨网段。" * 5
        )
        middle = (
            "第二部分：跨网段通信需要把数据交给默认网关，由路由器按路由表继续转发。"
            "静态路由和动态路由都可以提供下一跳。" * 3
        )
        tail = (
            "第三部分：TCP 快速重传触发条件。接收端连续收到三个重复 ACK 时，"
            "发送端不等待重传计时器到期，立即重传丢失的报文段；"
            "三个重复 ACK 必须针对同一报文段，且接收窗口不得为零。"
        )
        text = "\n\n".join([prefix, middle, tail])
        assert text.find("TCP 快速重传") > 350
        path.write_bytes(text.encode("utf-8"))
    return path.read_bytes().decode("utf-8")


def _wait_kb(kb_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = api("GET", f"/api/knowledge-bases/{kb_id}")
        states = {item["status"] for item in last.get("items") or []}
        if states and states <= {"ready", "failed"}:
            return last
        time.sleep(0.3)
    raise SystemExit(f"kb not ready: {last}")


def _start_diag(kb_id: str, source_id: str) -> dict:
    return api(
        "POST",
        f"/api/sources/{source_id}/diagnosis/sessions",
        json={
            "idempotency_key": f"restart-dx-{time.time_ns()}",
            "goal": GOAL,
            "learning_mode": "auto",
            "knowledge_base_id": kb_id,
            "scope_mode": "all",
        },
    )["session"]


def _answer_all(session: dict) -> dict:
    for _ in range(10):
        question = session.get("current_question")
        if not question:
            return session
        session = api(
            "POST",
            f"/api/diagnosis/sessions/{session['id']}/answers",
            json={
                "idempotency_key": f"restart-ans-{time.time_ns()}",
                "question_id": question["id"],
                "selected_index": 0,
                "status": "answered",
            },
        )
    return session


def _finish(session_id: str) -> dict:
    return api(
        "POST",
        f"/api/diagnosis/sessions/{session_id}/finish",
        json={"goal": GOAL, "learning_mode": "auto"},
    )


def _rewrite_plan_to_three_batches(version: dict) -> dict:
    items = list(version.get("items") or [])
    if len(items) < 3:
        raise SystemExit(f"need at least 3 plan points, got {len(items)}")
    selected = items[:3]
    modules = []
    for index, item in enumerate(selected, start=1):
        point_id = item.get("knowledge_point_id")
        name = item.get("display_name") or f"考点{index}"
        modules.append(
            {
                "module_id": f"mod_restart_{index}",
                "ordinal": index,
                "title": name,
                "objective": f"掌握{name}",
                "core_points": [name],
                "knowledge_point_ids": [point_id],
                "source_ids": [spec["source_id"] for spec in version.get("sources") or []],
                "suggested_question_count": 4,
                "completion_standard": "完成本模块练习。",
                "point_allocation": [
                    {
                        "knowledge_point_id": point_id,
                        "display_name": name,
                        "suggested_question_count": 4,
                    }
                ],
            }
        )
    for item in selected:
        item["suggested_question_count"] = 4
    con = sqlite3.connect(DB)
    con.execute(
        "update learning_plan_version set items_json=?, modules_json=?, question_count=?, status='confirmed' where id=?",
        (
            json.dumps(selected, ensure_ascii=False),
            json.dumps(modules, ensure_ascii=False),
            12,
            version["id"],
        ),
    )
    con.commit()
    con.close()
    api(
        "POST",
        f"/api/plans/{version['plan_id']}/versions/{version['id']}/confirm",
    )
    return {"items": selected, "modules": modules, "question_count": 12}


def phase_start() -> None:
    text = _text()
    kb = api("POST", "/api/knowledge-bases", json={"name": "重启验收 3 批", "description": ""})
    upload = api(
        "POST",
        f"/api/knowledge-bases/{kb['id']}/items/upload",
        files=[("files", ("three_topics.txt", text.encode("utf-8"), "text/plain"))],
    )
    detail = _wait_kb(kb["id"])
    item = detail["items"][0]
    session = _answer_all(_start_diag(kb["id"], item["source_id"]))
    preview = _finish(session["id"])
    version = preview["version"]
    plan = _rewrite_plan_to_three_batches(version)
    confirmed = api(
        "POST",
        f"/api/diagnosis/sessions/{session['id']}/confirm",
        json={"idempotency_key": f"restart-confirm-{time.time_ns()}"},
    )
    job_id = confirmed["job_id"]
    state = {
        "kb_id": kb["id"],
        "source_id": item["source_id"],
        "session_id": session["id"],
        "version_id": version["id"],
        "plan_version_id": version["id"],
        "old_job_id": job_id,
        "plan": plan,
    }
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    deadline = time.time() + 300
    cached = 0
    while time.time() < deadline:
        con = sqlite3.connect(DB)
        cached = con.execute("select count(*) from generation_batch_cache").fetchone()[0]
        con.close()
        job = api("GET", f"/api/jobs/{job_id}")
        print("waiting", cached, job.get("status"), job.get("stage"), flush=True)
        if cached >= 1:
            break
        if job.get("status") in {"failed", "insufficient_evidence", "succeeded"}:
            break
        time.sleep(0.2)
    state["cached_before_kill"] = cached
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))


def _wait_job(job_id: str, timeout: float = 900.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = api("GET", f"/api/jobs/{job_id}")
        print("job", last.get("status"), last.get("stage"), last.get("elapsed_ms"), flush=True)
        if last["status"] in {"succeeded", "failed", "insufficient_evidence", "cancelled"}:
            return last
        time.sleep(1)
    raise SystemExit(f"job timeout: {last}")


def phase_resume() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    session_id = state["session_id"]
    old_job = state["old_job_id"]
    confirmed = api(
        "POST",
        f"/api/diagnosis/sessions/{session_id}/confirm",
        json={"idempotency_key": f"restart-retry-{time.time_ns()}"},
    )
    new_job = confirmed.get("job_id")
    if not new_job:
        raise SystemExit(f"no new job: {confirmed}")
    job = _wait_job(new_job)
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select batch_index, job_id, fingerprint from generation_batch_cache order by batch_index"
    ).fetchall()
    pack_id = con.execute(
        "select pack_id from generation_job where id=?", (new_job,)
    ).fetchone()
    con.close()
    pack = api("GET", f"/api/packs/{job['pack_id']}") if job.get("pack_id") else {}
    questions = pack.get("questions") or []
    state.update(
        {
            "new_job_id": new_job,
            "cached_batches": [{"batch": row[0], "job_id": row[1]} for row in rows],
            "pack_id": job.get("pack_id"),
            "delivered_total": len(questions),
            "modules": len(pack.get("modules") or []),
        }
    )
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    assert job["status"] == "succeeded", job
    assert len(questions) == 12
    assert any(row[0] == 1 and row[1] == old_job for row in rows)
    assert len({row[0] for row in rows}) == 3


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in {"start", "resume"}:
        raise SystemExit("usage: acceptance_restart.py start|resume")
    if sys.argv[1] == "start":
        phase_start()
    else:
        phase_resume()