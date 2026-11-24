from __future__ import annotations

import time

import pymupdf
from sqlalchemy import select

from app.domain.models import Citation
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def _pdf(path, label: str) -> None:
    doc = pymupdf.open()
    for page_no in range(1, 4):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"{label} 第 {page_no} 页", fontsize=16)
        page.insert_text((72, 140), f"{label} 的正文证据，用于范围校验。", fontsize=11)
    doc.save(path)
    doc.close()


def _kb_with_two_pdfs(client, tmp_path) -> tuple[dict, dict, dict]:
    first = helpers.upload(client, _pdf_path(tmp_path, "alpha", "A"))
    second = helpers.upload(client, _pdf_path(tmp_path, "beta", "B"))
    kb = client.post("/api/knowledge-bases", json={"name": "范围库"}).json()
    for source in (first, second):
        added = client.post(
            f"/api/knowledge-bases/{kb['id']}/items", json={"source_id": source["id"]}
        )
        assert added.status_code == 201, added.text
    return kb, first, second


def _pdf_path(tmp_path, name: str, label: str):
    path = tmp_path / f"{name}.pdf"
    _pdf(path, label)
    return path


def _start(client, source_id: str, kb_id: str, **extra) -> dict:
    body = {
        "idempotency_key": f"scope-{time.time_ns()}",
        "goal": "范围校验",
        "learning_mode": "auto",
        "knowledge_base_id": kb_id,
        **extra,
    }
    response = client.post(f"/api/sources/{source_id}/diagnosis/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()["session"]


def _answer_all(client, session: dict) -> dict:
    for _ in range(8):
        question = session.get("current_question")
        if not question:
            return session
        session = client.post(
            f"/api/diagnosis/sessions/{session['id']}/answers",
            json={
                "idempotency_key": f"scope-ans-{time.time_ns()}",
                "question_id": question["id"],
                "selected_index": 0,
                "status": "answered",
            },
        ).json()
    return session


def _generate(client, session: dict) -> dict:
    preview = client.post(
        f"/api/diagnosis/sessions/{session['id']}/finish",
        json={"goal": "范围校验", "learning_mode": "auto"},
    )
    assert preview.status_code == 200, preview.text
    confirmed = client.post(
        f"/api/diagnosis/sessions/{session['id']}/confirm",
        json={"idempotency_key": f"scope-confirm-{time.time_ns()}"},
    )
    assert confirmed.status_code == 200, confirmed.text
    job = helpers.wait_job(client, confirmed.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    return job


def test_chapter_preset_only_selects_that_source_and_page_range(client, tmp_path):
    kb, first, second = _kb_with_two_pdfs(client, tmp_path)
    session = _start(
        client,
        first["id"],
        kb["id"],
        scope_mode="selected",
        source_ids=[first["id"]],
        page_ranges={first["id"]: {"page_start": 2, "page_end": 3}},
    )
    scope = session["source_scope"]
    assert [item["source_id"] for item in scope] == [first["id"]]
    assert scope[0]["page_start"] == 2 and scope[0]["page_end"] == 3

    session = _answer_all(client, session)
    job = _generate(client, session)
    with SessionLocal() as db:
        citations = db.execute(
            select(Citation).where(Citation.pack_id == job["pack_id"])
        ).scalars().all()
    assert citations
    assert {row.source_id for row in citations} == {first["id"]}
    assert all(2 <= row.page_no <= 3 for row in citations if row.page_no is not None)


def test_whole_knowledge_base_ignores_stale_page_ranges(client, tmp_path):
    kb, first, second = _kb_with_two_pdfs(client, tmp_path)
    session = _start(
        client,
        first["id"],
        kb["id"],
        scope_mode="all",
        source_ids=[first["id"], second["id"]],
        page_ranges={first["id"]: {"page_start": 2, "page_end": 3}},
    )
    scope = session["source_scope"]
    assert {item["source_id"] for item in scope} == {first["id"], second["id"]}
    assert all(item["page_start"] is None and item["page_end"] is None for item in scope)

    session = _answer_all(client, session)
    job = _generate(client, session)
    with SessionLocal() as db:
        from app.domain.models import GenerationJob

        payload = db.get(GenerationJob, job["job_id"]).request_json
    import json

    sources = json.loads(payload)["sources"]
    assert {item["source_id"] for item in sources} == {first["id"], second["id"]}
    assert all(item["page_start"] is None and item["page_end"] is None for item in sources)