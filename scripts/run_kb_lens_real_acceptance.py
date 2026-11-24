from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import pymupdf
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data_kb_fix"
LENS_PDF = ROOT / "samples" / "lens_two_conditions.pdf"
BASE = os.getenv("XUNZHI_ACCEPTANCE_BASE", "http://127.0.0.1:8031")
LOG_PATH = ROOT / "server_8031.log"
OUT = ROOT / "kb_lens_real_acceptance.json"

os.environ.setdefault("XUNZHI_DATA_DIR", str(DATA_DIR))
sys.path.insert(0, str(ROOT))

from app.domain.models import Evidence, SourceMaterial  # noqa: E402
from app.infra.db import SessionLocal  # noqa: E402
from app.infra.source_service import ensure_pdf_region_evidence  # noqa: E402

client = httpx.Client(base_url=BASE, timeout=180.0)


def api(method: str, path: str, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {response.status_code} {response.text[:800]}")
    return response.json()


def unique_pdf(tag: str) -> Path:
    assert LENS_PDF.exists(), "请先运行 python scripts/make_lens_acceptance_pdf.py"
    doc = pymupdf.open(str(LENS_PDF))
    doc[0].insert_text((20, 575), f"{tag}-{time.time_ns()}", fontsize=8)
    target = DATA_DIR / "tmp" / f"{tag}_{time.time_ns()}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(target))
    doc.close()
    return target


def create_kb(name: str) -> str:
    return api("POST", "/api/knowledge-bases", json={"name": name, "description": "透镜双图修复验收"})["id"]


def upload_pdf(kb_id: str, path: Path, timeout: float = 300.0) -> tuple[str, dict]:
    with path.open("rb") as fh:
        uploaded = api(
            "POST",
            f"/api/knowledge-bases/{kb_id}/items/upload",
            files={"files": (path.name, fh, "application/pdf")},
        )
    if uploaded.get("upload_errors"):
        raise RuntimeError(f"上传失败：{uploaded['upload_errors']}")
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = api("GET", f"/api/knowledge-bases/{kb_id}")
        items = last.get("items") or []
        states = {item["status"] for item in items}
        if states and states <= {"ready", "failed"}:
            item = next(item for item in items if item["status"] == "ready")
            return item["source_id"], item
        time.sleep(1)
    raise RuntimeError(f"知识库整理超时：{last}")


def start_diagnosis(kb_id: str, source_id: str) -> dict:
    body = {
        "idempotency_key": f"kb-lens-dx-{time.time_ns()}",
        "goal": "对照两页透镜原图学习：第 1 页 u > 2f，第 2 页 f < u < 2f。",
        "learning_mode": "auto",
        "knowledge_base_id": kb_id,
        "scope_mode": "all",
        "source_ids": [source_id],
        "page_ranges": {source_id: {"page_start": 1, "page_end": 2}},
    }
    return api("POST", f"/api/sources/{source_id}/diagnosis/sessions", json=body)["session"]


def answer_all(session: dict, rounds: int = 14) -> dict:
    for _ in range(rounds):
        question = session.get("current_question")
        if not question:
            return session
        session = api(
            "POST",
            f"/api/diagnosis/sessions/{session['id']}/answers",
            json={
                "idempotency_key": f"kb-lens-ans-{time.time_ns()}",
                "question_id": question["id"],
                "selected_index": 0,
                "status": "answered",
            },
        )
    raise RuntimeError("诊断轮次超限")


def finish_and_confirm(session_id: str) -> tuple[dict, dict]:
    preview = api(
        "POST",
        f"/api/diagnosis/sessions/{session_id}/finish",
        json={"goal": "对照两页透镜原图学习", "learning_mode": "auto"},
    )
    confirmed = api(
        "POST",
        f"/api/diagnosis/sessions/{session_id}/confirm",
        json={"idempotency_key": f"kb-lens-confirm-{time.time_ns()}"},
    )
    job_id = confirmed.get("job_id")
    if not job_id:
        raise RuntimeError(f"确认后没有生成任务：{confirmed}")
    deadline = time.time() + 1800
    job = {}
    while time.time() < deadline:
        job = api("GET", f"/api/jobs/{job_id}")
        if job["status"] in {"succeeded", "failed", "insufficient_evidence", "cancelled"}:
            break
        time.sleep(3)
    job["_job_id"] = job_id
    return preview, job


def pack_report(pack_id: str) -> dict:
    pack = api("GET", f"/api/packs/{pack_id}")
    rows = []
    for module in pack.get("modules") or []:
        for page in module.get("pages") or []:
            visual = page.get("visual") or {}
            if page.get("page_type") != "source_asset":
                continue
            url = visual.get("asset_url")
            status = None
            digest = None
            width = height = None
            response = client.get(f"{BASE}{url}")
            status = response.status_code
            if status == 200:
                data = response.content
                digest = hashlib.sha256(data).hexdigest()[:16]
                with Image.open(io.BytesIO(data)) as img:
                    width, height = img.size
            rows.append(
                {
                    "module_id": module.get("module_id"),
                    "page_id": page.get("page_id"),
                    "title": page.get("title"),
                    "source_ref": visual.get("source_ref"),
                    "citation_id": visual.get("citation_id"),
                    "source_name": visual.get("source_name"),
                    "physical_page": visual.get("page_no"),
                    "boxes": visual.get("boxes"),
                    "asset_url": url,
                    "http_status": status,
                    "sha256_16": digest,
                    "width": width,
                    "height": height,
                    "explanation": page.get("explanation"),
                }
            )
    return {
        "pack_id": pack_id,
        "question_count": pack.get("question_count"),
        "module_count": len(pack.get("modules") or []),
        "modules": [
            {
                "module_id": module.get("module_id"),
                "title": module.get("title"),
                "pages": len(module.get("pages") or []),
                "recap_rules": len((module.get("recap") or {}).get("rules") or []),
            }
            for module in pack.get("modules") or []
        ],
        "source_asset_pages": rows,
        "two_pages_two_images": len(rows) >= 2 and len({row["sha256_16"] for row in rows}) >= 2,
    }


def evidence_rows(source_id: str) -> dict:
    db_path = (DATA_DIR / "xunzhi.sqlite3").as_uri() + "?mode=ro"
    con = sqlite3.connect(db_path, uri=True)
    try:
        regions = con.execute(
            "select page_no, locator_json from evidence where source_id=? and kind='image_region' and status='active'",
            (source_id,),
        ).fetchall()
        assets = con.execute(
            "select count(*) from image_asset where source_id=?", (source_id,)
        ).fetchone()[0]
        pages = con.execute(
            "select page_no, page_image_asset_id from source_page where source_id=? order by page_no",
            (source_id,),
        ).fetchall()
        marker = con.execute(
            "select value from app_setting where key=?", (f"pdf.region_index.{source_id}",)
        ).fetchone()
        return {
            "regions": len(regions),
            "region_pages": sorted({row[0] for row in regions}),
            "assets": assets,
            "pages": [{"page_no": row[0], "asset_id": row[1]} for row in pages],
            "region_index": marker[0] if marker else None,
        }
    finally:
        con.close()


def drop_regions(source_id: str) -> None:
    con = sqlite3.connect(str(DATA_DIR / "xunzhi.sqlite3"), timeout=30)
    try:
        con.execute(
            "delete from evidence where source_id=? and kind='image_region'", (source_id,)
        )
        con.execute("delete from app_setting where key=?", (f"pdf.region_index.{source_id}",))
        con.commit()
    finally:
        con.close()


def log_lines(job_id: str) -> list[str]:
    if not LOG_PATH.exists():
        return []
    lines = []
    for line in LOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        if job_id in line and (
            "batch sent" in line
            or "batch failed" in line
            or "model request sent" in line
            or "model evidence stats" in line
        ):
            lines.append(line.strip())
    return lines[-8:]


def close_clients() -> None:
    client.close()
    try:
        from app.infra.db import engine

        engine.dispose()
    except Exception:
        pass


def main() -> int:
    result: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # 1. 新知识库 → 上传 → 选择该库学习 → 确认计划 → 生成
    kb_a = create_kb(f"透镜新库-{time.time_ns()}")
    pdf_a = unique_pdf("lens_new")
    source_a, item_a = upload_pdf(kb_a, pdf_a)
    before_a = evidence_rows(source_a)
    session_a = answer_all(start_diagnosis(kb_a, source_a))
    preview_a, job_a = finish_and_confirm(session_a["id"])
    result["new_kb"] = {
        "knowledge_base_id": kb_a,
        "source_id": source_a,
        "upload_item": item_a,
        "before_generation": before_a,
        "session_id": session_a["id"],
        "plan_version_id": (preview_a.get("version") or {}).get("id"),
        "plan_question_count": (preview_a.get("version") or {}).get("question_count"),
        "plan_modules": [
            {
                "module_id": module.get("module_id"),
                "title": module.get("title"),
                "page_start": module.get("page_start"),
                "page_end": module.get("page_end"),
                "core_points": module.get("core_points"),
            }
            for module in (preview_a.get("version") or {}).get("modules") or []
        ],
        "job": job_a,
        "request_log": log_lines(job_a.get("_job_id") or ""),
        "after_generation": evidence_rows(source_a),
    }
    if job_a.get("status") == "succeeded":
        result["new_kb"]["pack"] = pack_report(job_a["pack_id"])

    # 2. 旧库样例：上传后删除区域证据与索引标记，模拟旧入库，再生成验证补建
    kb_b = create_kb(f"透镜旧库-{time.time_ns()}")
    pdf_b = unique_pdf("lens_legacy")
    source_b, item_b = upload_pdf(kb_b, pdf_b)
    drop_regions(source_b)
    before_b = evidence_rows(source_b)
    session_b = answer_all(start_diagnosis(kb_b, source_b))
    preview_b, job_b = finish_and_confirm(session_b["id"])
    result["legacy_kb"] = {
        "knowledge_base_id": kb_b,
        "source_id": source_b,
        "upload_item": item_b,
        "after_simulated_legacy": before_b,
        "session_id": session_b["id"],
        "plan_version_id": (preview_b.get("version") or {}).get("id"),
        "job": job_b,
        "request_log": log_lines(job_b.get("_job_id") or ""),
        "after_generation": evidence_rows(source_b),
    }
    if job_b.get("status") == "succeeded":
        result["legacy_kb"]["pack"] = pack_report(job_b["pack_id"])

        with SessionLocal() as db:
            material = db.get(SourceMaterial, source_b)
            added1 = ensure_pdf_region_evidence(db, material)
            added2 = ensure_pdf_region_evidence(db, material)
        result["legacy_kb"]["repeat_ensure"] = {
            "added_first": added1,
            "added_second": added2,
            "after_repeat": evidence_rows(source_b),
        }

    result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    close_clients()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())