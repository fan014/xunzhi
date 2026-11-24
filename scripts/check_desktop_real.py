from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
PDF = ROOT / "tests" / "fixtures" / "pdf" / "sample_3pages.pdf"
REPORT = ROOT / "desktop_real_acceptance.json"
SHOTS = ROOT / "screenshots"


def api(client: httpx.Client, method: str, path: str, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {response.status_code} {response.text[:800]}")
    return response.json()


def pick_kb(client: httpx.Client) -> str:
    payload = api(client, "GET", "/api/knowledge-bases")
    bases = payload.get("items") if isinstance(payload, dict) else payload
    if not bases:
        raise RuntimeError("本机没有知识库")
    return bases[0]["id"]


def ensure_pdf(client: httpx.Client, kb_id: str) -> str:
    detail = api(client, "GET", f"/api/knowledge-bases/{kb_id}")
    for item in detail.get("items") or []:
        if item.get("display_name") == PDF.name and item.get("source_id"):
            return item["source_id"]
    with PDF.open("rb") as fh:
        payload = api(
            client,
            "POST",
            f"/api/knowledge-bases/{kb_id}/items/upload",
            files={"files": (PDF.name, fh, "application/pdf")},
        )
    if payload.get("upload_errors"):
        raise RuntimeError(f"上传失败：{payload['upload_errors']}")
    for _ in range(120):
        detail = api(client, "GET", f"/api/knowledge-bases/{kb_id}")
        item = next(
            (row for row in detail.get("items") or [] if row.get("display_name") == PDF.name),
            None,
        )
        if item and item.get("status") == "ready" and item.get("source_id"):
            return item["source_id"]
        if item and item.get("status") == "failed":
            raise RuntimeError(f"PDF 整理失败：{item}")
        time.sleep(1)
    raise RuntimeError("PDF 整理超时")


def start_diagnosis(client: httpx.Client, kb_id: str, source_id: str) -> dict:
    return api(
        client,
        "POST",
        f"/api/sources/{source_id}/diagnosis/sessions",
        json={
            "idempotency_key": f"desktop-real-{time.time_ns()}",
            "goal": "学习这份公开测试 PDF 的核心概念",
            "learning_mode": "foundation",
            "knowledge_base_id": kb_id,
            "scope_mode": "source",
            "source_ids": [source_id],
        },
    )


def answer_all(client: httpx.Client, session: dict) -> dict:
    for _ in range(20):
        if session.get("current_question"):
            question = session["current_question"]
            session = api(
                client,
                "POST",
                f"/api/diagnosis/sessions/{session['id']}/answers",
                json={
                    "idempotency_key": f"desktop-ans-{time.time_ns()}",
                    "question_id": question["id"],
                    "selected_index": 0,
                    "status": "answered",
                },
            )
            if session.get("error"):
                session = api(
                    client, "POST", f"/api/diagnosis/sessions/{session['id']}/retry"
                )
            continue
        if session.get("error"):
            session = api(
                client, "POST", f"/api/diagnosis/sessions/{session['id']}/retry"
            )
            continue
        return session
    raise RuntimeError("诊断轮次过多")


def wait_job(client: httpx.Client, job_id: str, timeout: float = 900.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = api(client, "GET", f"/api/jobs/{job_id}")
        if last["status"] in {"succeeded", "failed", "cancelled", "insufficient_evidence"}:
            return last
        time.sleep(2)
    raise RuntimeError(f"生成超时：{last}")


def first_citation(pack: dict) -> dict | None:
    for module in pack.get("modules") or []:
        for section in module.get("handout") or []:
            for citation in section.get("citations") or []:
                return citation
    for question in pack.get("questions") or []:
        for citation in question.get("citations") or []:
            return citation
    return None


def click_citation(pack_id: str, citation_label: str | None) -> dict:
    SHOTS.mkdir(exist_ok=True)
    options = webdriver.EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,900")
    driver = webdriver.Edge(options=options)
    try:
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        wait = WebDriverWait(driver, 30)
        driver.get(f"{BASE}/packs/{pack_id}")
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "button.chip"))
        chips = driver.find_elements(By.CSS_SELECTOR, "button.chip")
        target = chips[0]
        if citation_label:
            for chip in chips:
                if chip.text == citation_label:
                    target = chip
                    break
        target.click()
        wait.until(lambda d: d.find_element(By.ID, "citation-overlay").is_displayed())
        body = driver.find_element(By.ID, "citation-body").text
        driver.save_screenshot(str(SHOTS / "desktop_real_citation.png"))
        return {
            "chip_count": len(chips),
            "clicked_label": target.text,
            "overlay_body": body[:500],
            "overlay_visible": True,
        }
    finally:
        driver.quit()


def main() -> int:
    result: dict = {"base": BASE}
    with httpx.Client(base_url=BASE, timeout=120.0) as client:
        kb_id = pick_kb(client)
        result["knowledge_base_id"] = kb_id
        result["knowledge_base_count"] = len(api(client, "GET", "/api/knowledge-bases").get("items") or [])
        source_id = ensure_pdf(client, kb_id)
        result["source_id"] = source_id
        session = start_diagnosis(client, kb_id, source_id)["session"]
        result["session_id"] = session["id"]
        session = answer_all(client, session)
        result["diagnostic_questions"] = session.get("current_round")
        preview = api(
            client,
            "POST",
            f"/api/diagnosis/sessions/{session['id']}/finish",
            json={"goal": "学习这份公开测试 PDF 的核心概念", "learning_mode": "foundation"},
        )
        version = preview["version"]
        result["plan"] = {
            "version_id": version["id"],
            "question_count": version.get("question_count"),
            "module_count": len(version.get("modules") or []),
            "modules": [
                {
                    "title": module.get("title"),
                    "core_points": module.get("core_points"),
                    "source_names": module.get("source_names"),
                }
                for module in version.get("modules") or []
            ],
        }
        confirmed = api(
            client,
            "POST",
            f"/api/diagnosis/sessions/{session['id']}/confirm",
            json={"idempotency_key": f"desktop-confirm-{time.time_ns()}"},
        )
        job = wait_job(client, confirmed["job_id"])
        result["job"] = {
            "job_id": job.get("job_id"),
            "status": job.get("status"),
            "error": job.get("error"),
        }
        if job.get("status") != "succeeded":
            REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError(f"真实生成失败：{job}")
        pack = api(client, "GET", f"/api/packs/{job['pack_id']}")
        result["pack"] = {
            "pack_id": pack.get("id"),
            "question_count": pack.get("question_count"),
            "module_count": len(pack.get("modules") or []),
            "question_module_ids": sorted(
                {question.get("module_id") for question in pack.get("questions") or []}
            ),
        }
        citation = first_citation(pack)
        result["citation"] = {
            "id": (citation or {}).get("id"),
            "label": (citation or {}).get("label"),
            "page_no": (citation or {}).get("page_no"),
        }
        if citation:
            result["citation_api"] = api(client, "GET", f"/api/citations/{citation['id']}")
            result["citation_ui"] = click_citation(pack["id"], citation.get("label"))
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())