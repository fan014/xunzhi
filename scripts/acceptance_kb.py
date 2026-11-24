from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8010"
PDF_DIR = Path("acceptance_pdfs")
STATE = Path("acceptance_state.json")
PDFS = sorted(PDF_DIR.glob("*.pdf"))
UNIQUE_TEXTS = ["快速重传", "三个重复 ACK", "路由反射器", "CLUSTER_LIST", "簇 ID"]

client = httpx.Client(base_url=BASE, timeout=120.0)


def api(method: str, path: str, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise SystemExit(f"{method} {path} -> {response.status_code} {response.text[:500]}")
    return response.json()


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def create_kb(name: str) -> dict:
    return api("POST", "/api/knowledge-bases", json={"name": name, "description": "公开验收数据"})


def upload_pdfs(kb_id: str) -> dict:
    files = []
    for path in PDFS:
        files.append(("files", (path.name, path.read_bytes(), "application/pdf")))
    return api("POST", f"/api/knowledge-bases/{kb_id}/items/upload", files=files)


def upload_text_source(path: Path, text: str) -> dict:
    path.write_text(text, encoding="utf-8")
    with path.open("rb") as fh:
        return api("POST", "/api/sources", files={"file": (path.name, fh, "text/plain")})["source"]


def wait_kb(kb_id: str, timeout: float = 900.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = api("GET", f"/api/knowledge-bases/{kb_id}")
        states = {item["status"] for item in last.get("items") or []}
        if states and states <= {"ready", "failed"}:
            return last
        print("  ready" if "ready" in states else "  processing", sum(1 for i in last.get("items") or [] if i["status"] == "ready"))
        time.sleep(2)
    raise SystemExit(f"knowledge base not ready: {last}")


def start_diagnosis(kb_id: str, source_id: str, goal: str, *, scope_mode="all", source_ids=None, page_ranges=None) -> dict:
    body = {
        "idempotency_key": f"acc-{time.time_ns()}",
        "goal": goal,
        "learning_mode": "auto",
        "knowledge_base_id": kb_id,
        "scope_mode": scope_mode,
    }
    if source_ids is not None:
        body["source_ids"] = source_ids
    if page_ranges is not None:
        body["page_ranges"] = page_ranges
    return api("POST", f"/api/sources/{source_id}/diagnosis/sessions", json=body)["session"]


def answer_all(session: dict, rounds: int = 12) -> dict:
    for _ in range(rounds):
        question = session.get("current_question")
        if not question:
            return session
        session = api(
            "POST",
            f"/api/diagnosis/sessions/{session['id']}/answers",
            json={
                "idempotency_key": f"acc-ans-{time.time_ns()}",
                "question_id": question["id"],
                "selected_index": 0,
                "status": "answered",
            },
        )
    raise SystemExit("diagnosis did not finish in allowed rounds")


def finish_preview(session_id: str, goal: str, question_count: int | None = None) -> dict:
    body = {"goal": goal, "learning_mode": "auto"}
    if question_count is not None:
        body["question_count"] = question_count
    return api("POST", f"/api/diagnosis/sessions/{session_id}/finish", json=body)


def confirm_and_generate(session_id: str, timeout: float = 1800.0, retries: int = 2) -> dict:
    last = {}
    for attempt in range(retries + 1):
        confirmed = api(
            "POST",
            f"/api/diagnosis/sessions/{session_id}/confirm",
            json={"idempotency_key": f"acc-confirm-{time.time_ns()}"},
        )
        job_id = confirmed.get("job_id")
        if not job_id:
            raise SystemExit(f"confirm returned no job: {confirmed}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            last = api("GET", f"/api/jobs/{job_id}")
            print("  job", last.get("status"), last.get("stage"), last.get("elapsed_ms"))
            if last["status"] in {"succeeded", "failed", "insufficient_evidence", "cancelled"}:
                break
            time.sleep(3)
        if last.get("status") == "succeeded":
            return last
        if attempt < retries and last.get("retry_available"):
            print("  retry generation", last.get("error"))
            continue
        return last
    return last


def pack_citations(pack: dict) -> list[dict]:
    rows = []
    for section in pack.get("handout") or []:
        for citation in section.get("citations") or []:
            rows.append({**citation, "owner": section.get("heading")})
    for question in pack.get("questions") or []:
        for citation in question.get("citations") or []:
            rows.append({**citation, "owner": question.get("stem")})
    return rows


def text_of_pack(pack: dict) -> str:
    parts = []
    for section in pack.get("handout") or []:
        parts.append(section.get("heading") or "")
        parts.append(section.get("body") or "")
    for module in pack.get("modules") or []:
        parts.append(json.dumps(module, ensure_ascii=False))
    for question in pack.get("questions") or []:
        parts.append(question.get("stem") or "")
        parts.append(question.get("explanation") or "")
    return "\n".join(parts)


def phase_main(state: dict) -> None:
    if state.get("kb_id"):
        detail = api("GET", f"/api/knowledge-bases/{state['kb_id']}")
    else:
        kb = create_kb("公开验收 517 页")
        state["kb_id"] = kb["id"]
        upload_pdfs(kb["id"])
        save_state(state)
        detail = wait_kb(kb["id"])
    print("kb", detail["item_count"], detail["ready_count"], detail["total_pages"], detail["failed_count"])
    assert detail["total_pages"] == 517, detail
    sources = {item["display_name"].split(".")[0]: item for item in detail["items"]}
    state["sources"] = {item["display_name"]: {"item_id": item["id"], "source_id": item["source_id"], "page_count": item["page_count"], "chapters": item["chapters"]} for item in detail["items"]}
    save_state(state)

    first = next(item for item in detail["items"] if item["display_name"].startswith("course_1"))
    goal = "整库学习五份网络课程，重点弄清传输层可靠传输和 BGP 路由反射。"
    session = answer_all(start_diagnosis(state["kb_id"], first["source_id"], goal))
    state["session_id"] = session["id"]
    save_state(state)
    preview = finish_preview(state["session_id"], goal, question_count=8)
    version = preview["version"]
    state["preview"] = {"question_count": version.get("question_count"), "modules": len(version.get("modules") or [])}
    save_state(state)
    print("preview", state["preview"])
    print("module sources", sorted({sid for m in version.get("modules") or [] for sid in m.get("source_ids") or []}))

    job = confirm_and_generate(state["session_id"])
    print("job", job)
    if job["status"] != "succeeded":
        raise SystemExit(f"generation failed: {job}")
    pack = api("GET", f"/api/packs/{job['pack_id']}")
    state["pack_id"] = job["pack_id"]
    save_state(state)
    text = text_of_pack(pack)
    citations = pack_citations(pack)
    hits = {keyword: keyword in text for keyword in UNIQUE_TEXTS}
    late = [
        {"source": c.get("source_name"), "page": c.get("page_no"), "quote": (c.get("quote") or "")[:120], "owner": c.get("owner")}
        for c in citations
        if c.get("source_name", "").startswith(("course_2", "course_5"))
        and c.get("page_no") in range(60, 90)
    ]
    state["pack_check"] = {
        "pack_id": job["pack_id"],
        "questions": len(pack.get("questions") or []),
        "modules": len(pack.get("modules") or []),
        "keyword_hits": hits,
        "late_citations": late,
        "citation_count": len(citations),
    }
    save_state(state)
    print(json.dumps(state["pack_check"], ensure_ascii=False, indent=2))


def phase_second(state: dict) -> None:
    if not state.get("kb2_id"):
        kb = create_kb("串库检查")
        state["kb2_id"] = kb["id"]
        save_state(state)
    kb = api("GET", f"/api/knowledge-bases/{state['kb2_id']}")
    if not kb.get("items"):
        source = upload_text_source(Path("acceptance_pdfs") / "second_only.txt", "串库独有知识点：OSPF 邻居状态机只在本库出现，用于检查是否串库。")
        api("POST", f"/api/knowledge-bases/{state['kb2_id']}/items", json={"source_id": source["id"]})
    detail = wait_kb(state["kb2_id"])
    source_id = detail["items"][0]["source_id"]
    goal = "只学习第二库里的 OSPF 邻居状态机。"
    session = answer_all(start_diagnosis(state["kb2_id"], source_id, goal))
    preview = finish_preview(session["id"], goal)
    version = preview["version"]
    module_sources = sorted({sid for m in version.get("modules") or [] for sid in m.get("source_ids") or []})
    state["second_check"] = {"module_sources": module_sources, "only_source": source_id, "preview": version.get("question_count")}
    save_state(state)
    print("second", state["second_check"])


def phase_scope(state: dict) -> None:
    detail = api("GET", f"/api/knowledge-bases/{state['kb_id']}")
    item = next(item for item in detail["items"] if item["display_name"].startswith("course_2"))
    chapter = next((c for c in item["chapters"] if c["page_start"] <= 67 <= c["page_end"]), None)
    assert chapter, item["chapters"]
    goal = f"只学「{chapter['title']}」这一章。"
    session = answer_all(
        start_diagnosis(
            state["kb_id"],
            item["source_id"],
            goal,
            scope_mode="selected",
            source_ids=[item["source_id"]],
            page_ranges={item["source_id"]: {"page_start": chapter["page_start"], "page_end": chapter["page_end"]}},
        )
    )
    preview = finish_preview(session["id"], goal)
    version = preview["version"]
    state["scope_check"] = {
        "chapter": chapter,
        "module_sources": sorted({sid for m in version.get("modules") or [] for sid in m.get("source_ids") or []}),
        "question_count": version.get("question_count"),
    }
    save_state(state)
    print("scope", json.dumps(state["scope_check"], ensure_ascii=False))


def phase_replace(state: dict) -> None:
    pack = api("GET", f"/api/packs/{state['pack_id']}")
    citations = pack_citations(pack)
    target_name = next(
        (c["source_name"] for c in citations if c.get("source_name", "").startswith("course_")),
        None,
    )
    assert target_name, [c.get("source_name") for c in citations][:10]
    detail = api("GET", f"/api/knowledge-bases/{state['kb_id']}")
    item = next(row for row in detail["items"] if row["display_name"] == target_name)
    source_id = item["source_id"]

    kb2 = api("GET", f"/api/knowledge-bases/{state['kb2_id']}")
    if not any(row["source_id"] == source_id for row in kb2.get("items") or []):
        api("POST", f"/api/knowledge-bases/{state['kb2_id']}/items", json={"source_id": source_id})

    old_citations = [
        {"page": c.get("page_no"), "quote": c.get("quote"), "source_name": c.get("source_name")}
        for c in citations
        if c.get("source_name") == target_name
    ]

    renamed = api(
        "PATCH",
        f"/api/knowledge-bases/{state['kb_id']}/items/{item['id']}",
        json={"display_name": "A 库别名"},
    )
    item_a = next(row for row in renamed["items"] if row["id"] == item["id"])
    kb2_after = api("GET", f"/api/knowledge-bases/{state['kb2_id']}")
    item_b = next(row for row in kb2_after["items"] if row["source_id"] == source_id)
    sources = api("GET", "/api/sources")["items"]
    source_row = next(row for row in sources if row["id"] == source_id)

    broken = (Path("tests/fixtures/broken/fake.pdf")).read_bytes()
    api(
        "POST",
        f"/api/knowledge-bases/{state['kb_id']}/items/{item['id']}/replace",
        files={"file": ("broken.pdf", broken, "application/pdf")},
    )
    deadline = time.time() + 120
    failed_item = {}
    while time.time() < deadline:
        current = api("GET", f"/api/knowledge-bases/{state['kb_id']}")
        failed_item = next(row for row in current["items"] if row["id"] == item["id"])
        if failed_item["status"] == "failed":
            break
        time.sleep(1)

    replacement = "第一章 替换后的新版本：跨网段需要默认网关，路由器连接不同网段。"
    api(
        "POST",
        f"/api/knowledge-bases/{state['kb_id']}/items/{item['id']}/replace",
        files={"file": ("replacement.txt", replacement.encode("utf-8"), "text/plain")},
    )
    deadline = time.time() + 120
    ready_item = {}
    while time.time() < deadline:
        current = api("GET", f"/api/knowledge-bases/{state['kb_id']}")
        ready_item = next(row for row in current["items"] if row["id"] == item["id"])
        if ready_item["status"] == "ready" and ready_item["version_no"] == 2:
            break
        time.sleep(1)

    pack_after = api("GET", f"/api/packs/{state['pack_id']}")
    citations_after = pack_citations(pack_after)
    same = [
        {"page": c.get("page_no"), "quote": c.get("quote"), "source_name": c.get("source_name")}
        for c in citations_after
        if c.get("source_name") == target_name
    ]
    state["replace_check"] = {
        "target": target_name,
        "renamed_in_a": item_a["display_name"],
        "b_name": item_b["display_name"],
        "source_name": source_row["display_name"],
        "failed": {"status": failed_item.get("status"), "usable": failed_item.get("usable"), "source_id_same": failed_item.get("source_id") == source_id, "error": failed_item.get("error_message")},
        "ready": {"status": ready_item.get("status"), "usable": ready_item.get("usable"), "version_no": ready_item.get("version_no"), "source_changed": ready_item.get("source_id") != source_id},
        "old_citations_before": len(old_citations),
        "old_citations_after": len(same),
        "old_citations_equal": old_citations == same,
    }
    save_state(state)
    print(json.dumps(state["replace_check"], ensure_ascii=False, indent=2))


def phase_long(state: dict) -> None:
    import re
    from collections import Counter

    if not state.get("kb_long_id"):
        kb = create_kb("公开验收 46 题")
        state["kb_long_id"] = kb["id"]
        save_state(state)
        upload_pdfs(kb["id"])
    detail = wait_kb(state["kb_long_id"])
    print("kb", detail["item_count"], detail["ready_count"], detail["total_pages"], detail["failed_count"])
    assert detail["total_pages"] == 517, detail

    first = next(item for item in detail["items"] if item["display_name"].startswith("course_1"))
    goal = "整库学习五份网络课程，生成覆盖全部模块的长方案。"
    session = answer_all(start_diagnosis(state["kb_long_id"], first["source_id"], goal))
    preview = finish_preview(session["id"], goal)
    version = preview["version"]
    state["long_session_id"] = session["id"]
    state["long_version"] = version
    save_state(state)
    print("preview", version.get("question_count"), len(version.get("modules") or []))

    job = confirm_and_generate(session["id"])
    print("job", job)
    if job["status"] != "succeeded":
        raise SystemExit(f"long generation failed: {job.get('error')}")
    pack = api("GET", f"/api/packs/{job['pack_id']}")
    questions = pack.get("questions") or []
    modules = version.get("modules") or []
    point_to_module = {}
    for item in version.get("items") or []:
        point_to_module[item.get("display_name")] = item.get("module_id")
    for module in modules:
        for alloc in module.get("point_allocation") or []:
            point_to_module.setdefault(alloc.get("display_name"), module.get("module_id"))
    actual = Counter()
    unmapped = []
    for question in questions:
        module_id = point_to_module.get(question.get("knowledge_point"))
        if module_id:
            actual[module_id] += 1
        else:
            unmapped.append(question.get("knowledge_point"))
    expected = {
        module.get("module_id"): int(module.get("suggested_question_count") or 0)
        for module in modules
    }
    stems = [re.sub(r"[\s\W_]+", "", (question.get("stem") or "")).lower() for question in questions]
    state["long_check"] = {
        "pack_id": job["pack_id"],
        "planned_total": version.get("question_count"),
        "delivered_total": len(questions),
        "modules": len(modules),
        "expected_by_module": expected,
        "actual_by_module": dict(actual),
        "unmapped": unmapped,
        "duplicate_stems": len(stems) - len(set(stems)),
    }
    save_state(state)
    print(json.dumps(state["long_check"], ensure_ascii=False, indent=2))
    assert len(questions) == int(version.get("question_count") or 0), state["long_check"]
    assert dict(actual) == expected, state["long_check"]
    assert state["long_check"]["duplicate_stems"] == 0


def main() -> None:
    global STATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=["main", "second", "scope", "replace", "long", "all"])
    parser.add_argument("--state", default=str(STATE))
    args = parser.parse_args()
    STATE = Path(args.state)
    if not PDFS:
        raise SystemExit("run scripts/make_acceptance_pdfs.py first")
    status = api("GET", "/api/settings/model-status")
    print("model", {key: status.get(key) for key in ("configured", "is_test_double", "model")})
    state = load_state()
    phases = ["main", "second", "scope", "replace"] if args.phase == "all" else [args.phase]
    for phase in phases:
        print(f"== {phase} ==")
        globals()[f"phase_{phase}"](state)


if __name__ == "__main__":
    main()