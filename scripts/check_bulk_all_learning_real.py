from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import acceptance_kb as acc  # noqa: E402

OUT = ROOT / "bulk_all_learning_real.json"


def _start(kb_id: str, source_id: str) -> dict:
    body = {
        "idempotency_key": f"bulk-all-{time.time_ns()}",
        "goal": "全部学习",
        "learning_mode": "foundation",
        "knowledge_base_id": kb_id,
        "scope_mode": "all",
    }
    return acc.api(
        "POST", f"/api/sources/{source_id}/diagnosis/sessions", json=body
    )["session"]


def _finish(session_id: str) -> dict:
    return acc.api(
        "POST",
        f"/api/diagnosis/sessions/{session_id}/finish",
        json={"goal": "全部学习", "learning_mode": "foundation"},
    )


def main() -> int:
    state = acc.load_state()
    if state.get("kb_id"):
        detail = acc.api("GET", f"/api/knowledge-bases/{state['kb_id']}")
    else:
        kb = acc.create_kb("公开验收 517 页")
        state["kb_id"] = kb["id"]
        acc.upload_pdfs(kb["id"])
        acc.save_state(state)
        detail = acc.wait_kb(kb["id"])
    items = detail.get("items") or []
    assert items, detail
    first = next(
        (item for item in items if item["display_name"].startswith("course_1")),
        items[0],
    )
    session = acc.answer_all(_start(state["kb_id"], first["source_id"]))
    preview = _finish(session["id"])
    version = preview["version"]
    modules = version.get("modules") or []
    items_out = version.get("items") or []
    result = {
        "session_id": session["id"],
        "plan_version_id": version["id"],
        "module_count": len(modules),
        "question_count": version.get("question_count"),
        "pseudo_point_present": any(
            item.get("display_name") == "全部学习" for item in items_out
        ),
        "modules": [
            {
                "title": module.get("title"),
                "core_points": module.get("core_points"),
                "source_names": module.get("source_names"),
                "source_scopes": module.get("source_scopes"),
                "page_start": module.get("page_start"),
                "page_end": module.get("page_end"),
                "question_count": module.get("suggested_question_count"),
            }
            for module in modules
        ],
        "excluded_points": preview.get("excluded_points") or [],
    }
    assert not result["pseudo_point_present"], result
    assert result["module_count"] >= 3, result

    job = acc.confirm_and_generate(session["id"])
    result["job"] = job
    if job.get("status") != "succeeded":
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(f"generation failed: {job}")
    pack = acc.api("GET", f"/api/packs/{job['pack_id']}")
    result["pack"] = {
        "pack_id": pack.get("id"),
        "question_count": pack.get("question_count"),
        "module_count": len(pack.get("modules") or []),
        "citation_count": len(acc.pack_citations(pack)),
        "question_module_ids": sorted(
            {question.get("module_id") for question in pack.get("questions") or []}
        ),
        "source_pages": sorted(
            {
                (citation.get("source_name"), citation.get("page_no"))
                for citation in acc.pack_citations(pack)
                if citation.get("page_no") is not None
            }
        )[:20],
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())