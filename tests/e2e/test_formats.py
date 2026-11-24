from __future__ import annotations

import time

from tests.e2e.test_png_slice import upload, wait_job
from tests.fixtures import make_fixtures


def test_pdf_range_is_a_hard_input_boundary(client, sample_pdf):
    source = upload(client, sample_pdf)
    created = client.post(
        "/api/packs",
        json={
            "idempotency_key": "pdf-range-0001",
            "sources": [{"source_id": source["id"], "page_start": 2, "page_end": 2}],
            "progress_text": "我只想复习第二页的矢量图",
            "difficulty": "basic",
            "question_count": 3,
            "consent_ack": True,
        },
    )
    assert created.status_code == 202, created.text
    job = wait_job(client, created.json()["job_id"])
    assert job["status"] in {"failed", "insufficient_evidence"}, job
    # Fake 回放 3q_ok 引用 F1，本次只有 T/P 证据，因此必须被白名单拒绝，不产生学习包。
    assert client.get(f"/api/jobs/{job['job_id']}").json()["pack_id"] is None


def test_image_variants_are_stored_as_evidence(client):
    for name in ("topology.jpg", "topology.webp"):
        path = make_fixtures.IMAGES / name
        with open(path, "rb") as fh:
            response = client.post(
                "/api/sources",
                files={"file": (path.name, fh, "application/octet-stream")},
            )
        assert response.status_code == 201, response.text
        assert response.json()["source"]["kind"] == "image"