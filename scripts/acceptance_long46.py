from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import sys

sys.path.insert(0, str(Path("scripts").resolve()))

from acceptance_kb import (  # noqa: E402
    answer_all,
    api,
    confirm_and_generate,
    finish_preview,
    start_diagnosis,
)

BASE = "http://127.0.0.1:8010"
DB = Path("data_acceptance_long/xunzhi.sqlite3")
OUT = Path("acceptance_long46.json")

def normalize(value) -> str:
    return re.sub(r"[\s\W_]+", "", value or "").lower()
GOAL = "整库学习五份网络课程，生成覆盖全部模块的长方案。"


def _raise_plan_to_46(version_id: str) -> tuple[list[dict], list[dict]]:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    row = cur.execute(
        "select items_json, modules_json from learning_plan_version where id=?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise SystemExit("plan version not found")
    items = json.loads(row[0] or "[]")
    modules = json.loads(row[1] or "[]")
    first_points = {module["knowledge_point_ids"][0] for module in modules[:6]}
    for item in items:
        item["suggested_question_count"] = 3 if item["knowledge_point_id"] in first_points else 2
    for index, module in enumerate(modules):
        module["suggested_question_count"] = 5 if index < 6 else 4
        if module.get("point_allocation"):
            module["point_allocation"] = [
                {
                    **alloc,
                    "suggested_question_count": 3
                    if alloc.get("knowledge_point_id") in first_points
                    else 2,
                }
                for alloc in module["point_allocation"]
            ]
    total = sum(int(item.get("suggested_question_count") or 0) for item in items)
    assert total == 46, total
    cur.execute(
        "update learning_plan_version set items_json=?, modules_json=?, question_count=?, status='confirmed' where id=?",
        (
            json.dumps(items, ensure_ascii=False),
            json.dumps(modules, ensure_ascii=False),
            46,
            version_id,
        ),
    )
    con.commit()
    con.close()
    return items, modules


def main() -> None:
    state = json.loads(Path("acceptance_state_long.json").read_text(encoding="utf-8"))
    kb_id = state["kb_long_id"]
    detail = api("GET", f"/api/knowledge-bases/{kb_id}")
    first = next(item for item in detail["items"] if item["display_name"].startswith("course_1"))
    session = answer_all(start_diagnosis(kb_id, first["source_id"], GOAL))
    preview = finish_preview(session["id"], GOAL)
    version = preview["version"]
    plan_id = version["plan_id"]
    version_id = version["id"]
    print("auto plan", version.get("question_count"), len(version.get("modules") or []))

    items, modules = _raise_plan_to_46(version_id)
    api("POST", f"/api/plans/{plan_id}/versions/{version_id}/confirm")

    job = confirm_and_generate(session["id"])
    print("job", job)
    if job["status"] != "succeeded":
        raise SystemExit(f"long46 failed: {job.get('error')}")

    pack = api("GET", f"/api/packs/{job['pack_id']}")
    questions = pack.get("questions") or []
    con = sqlite3.connect(DB)
    rows = con.execute(
        "select knowledge_point, module_id, stem from question where pack_id=?",
        (job["pack_id"],),
    ).fetchall()
    con.close()

    point_module = {
        pid: module.get("module_id")
        for module in modules
        for pid in module.get("knowledge_point_ids") or []
    }
    name_module = {
        normalize(item.get("display_name")): point_module.get(item.get("knowledge_point_id"))
        for item in items
    }
    actual = Counter()
    unmapped = []
    module_ids = Counter()
    stems = []
    for knowledge_point, module_id, stem in rows:
        module_ids[module_id] += 1
        mapped = name_module.get(normalize(knowledge_point))
        if mapped:
            actual[mapped] += 1
        else:
            unmapped.append(knowledge_point)
        stems.append(normalize(stem))
    expected = {module["module_id"]: int(module["suggested_question_count"]) for module in modules}
    review_count = module_ids.get("review", 0)
    result = {
        "pack_id": job["pack_id"],
        "job_id": job["job_id"],
        "session_id": session["id"],
        "version_id": version_id,
        "planned_total": 46,
        "delivered_total": len(questions),
        "modules": len(modules),
        "expected_by_module": expected,
        "actual_by_module": dict(actual),
        "unmapped": unmapped,
        "duplicate_stems": len(stems) - len(set(stems)),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    assert len(questions) == 46, result
    assert len(rows) == 46, result
    assert dict(actual) == expected, result
    assert review_count <= 1, result
    assert result["duplicate_stems"] == 0, result


if __name__ == "__main__":
    main()