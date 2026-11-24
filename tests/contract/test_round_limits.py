from __future__ import annotations

from dataclasses import replace

import pymupdf

from app.api import routes
from tests.stage4_helpers import make_png, upload, wait_job


def spec(source, page_start=None, page_end=None):
    return {"source_id": source["id"], "page_start": page_start, "page_end": page_end}


def post_pack(client, sources, key: str):
    return client.post(
        "/api/packs",
        json={
            "idempotency_key": key,
            "sources": sources,
            "progress_text": "同一网段和跨网段发包容易混，想用这张图练一下。",
            "difficulty": "consolidate",
            "question_count": 3,
            "consent_ack": True,
        },
    )


def pack_count(client) -> int:
    return len(client.get("/api/packs?limit=200").json()["items"])


def cleanup_sources(client, sources) -> None:
    for source in sources:
        client.request("DELETE", f"/api/sources/{source['id']}", json={"confirm": True})


def make_pdf(path, seed: int) -> None:
    doc = pymupdf.open()
    for page_no in (1, 2):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Round limit PDF {seed} page {page_no}", fontsize=16)
    doc.save(path)
    doc.close()


def test_library_over_ten_can_still_upload_and_small_round_generates(client, tmp_path):
    uploaded = []
    pack_id = None
    try:
        for index in range(11):
            uploaded.append(
                upload(
                    client,
                    make_png(
                        tmp_path / f"library_{index}.png",
                        color=(20, 30 + index * 5, 40),
                    ),
                )
            )
        created = post_pack(client, [spec(uploaded[0])], "round-count-one-01")
        assert created.status_code == 202, created.text
        job = wait_job(client, created.json()["job_id"])
        assert job["status"] == "succeeded", job
        pack_id = job["pack_id"]

        before = pack_count(client)
        over = post_pack(client, [spec(item) for item in uploaded], "round-count-eleven-01")
        assert over.status_code == 413
        assert over.json()["error"]["code"] == "too_many_sources"
        assert pack_count(client) == before
    finally:
        if pack_id:
            client.request("DELETE", f"/api/packs/{pack_id}", json={"confirm": True})
        cleanup_sources(client, uploaded)


def test_duplicate_source_id_is_editable_422_without_job(client, tmp_path):
    source = upload(client, make_png(tmp_path / "duplicate_round.png", color=(60, 90, 120)))
    try:
        before = pack_count(client)
        response = post_pack(client, [spec(source), spec(source)], "round-duplicate-01")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "duplicate_source"
        assert pack_count(client) == before
    finally:
        cleanup_sources(client, [source])


def test_round_total_bytes_limit_rejects_before_job(client, tmp_path, monkeypatch):
    source = upload(client, make_png(tmp_path / "round_bytes.png", color=(80, 40, 100)))
    try:
        monkeypatch.setattr(routes, "settings", replace(routes.settings, max_total_mb=0))
        before = pack_count(client)
        response = post_pack(client, [spec(source)], "round-bytes-01")
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "too_large"
        assert "总大小" in response.json()["error"]["message"]
        assert pack_count(client) == before
    finally:
        cleanup_sources(client, [source])


def test_round_total_pdf_pages_limit_rejects_before_job(client, tmp_path, monkeypatch):
    pdf_a = tmp_path / "round_pages_a.pdf"
    pdf_b = tmp_path / "round_pages_b.pdf"
    make_pdf(pdf_a, 1)
    make_pdf(pdf_b, 2)
    source_a = upload(client, pdf_a)
    source_b = upload(client, pdf_b)
    try:
        monkeypatch.setattr(routes, "settings", replace(routes.settings, max_pages=1))
        before = pack_count(client)
        response = post_pack(
            client,
            [spec(source_a, 2, 2), spec(source_b, 2, 2)],
            "round-pages-01",
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "too_large"
        assert "页" in response.json()["error"]["message"]
        assert pack_count(client) == before
    finally:
        cleanup_sources(client, [source_a, source_b])
