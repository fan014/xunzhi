from __future__ import annotations

from pathlib import Path

from app.domain.models import Evidence, SourcePage
from app.infra.db import SessionLocal

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def upload(client, path: Path, name: str | None = None):
    with open(path, "rb") as fh:
        response = client.post(
            "/api/sources", files={"file": (name or path.name, fh, "application/octet-stream")}
        )
    return response


def test_all_six_formats_are_accepted(client):
    files = [
        FIXTURES / "images" / "topology.png",
        FIXTURES / "images" / "topology.jpg",
        FIXTURES / "images" / "topology.webp",
        FIXTURES / "text" / "note.txt",
        FIXTURES / "text" / "note.md",
        FIXTURES / "pdf" / "sample_3pages.pdf",
    ]
    kinds = set()
    for path in files:
        response = upload(client, path)
        assert response.status_code == 201, (path.name, response.text)
        payload = response.json()
        kinds.add(payload["source"]["kind"])
        assert payload["source"]["status"] == "active"
    assert kinds == {"image", "text", "pdf"}


def test_disguised_extension_is_rejected(client):
    response = upload(client, FIXTURES / "broken" / "fake.pdf")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "file_unreadable"


def test_empty_text_is_rejected(client):
    response = upload(client, FIXTURES / "broken" / "empty.txt")
    assert response.status_code == 422


def test_unsupported_format_is_rejected(client):
    response = upload(client, FIXTURES / "text" / "note.txt", name="note.docx")
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_format"


def test_duplicate_upload_is_reused(client):
    first = upload(client, FIXTURES / "images" / "topology.png")
    second = upload(client, FIXTURES / "images" / "topology.png")
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["source"]["id"] == second.json()["source"]["id"]
    assert second.json()["reused"] is True


def test_invalid_pdf_page_range_blocks_generation(client):
    uploaded = upload(client, FIXTURES / "pdf" / "sample_3pages.pdf").json()["source"]
    response = client.post(
        "/api/packs",
        json={
            "idempotency_key": "range-invalid-0001",
            "sources": [{"source_id": uploaded["id"], "page_start": 2, "page_end": 9}],
            "progress_text": "只想看第二页到第四页",
            "difficulty": "consolidate",
            "question_count": 3,
            "consent_ack": True,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_page_range"


def test_pdf_evidence_covers_text_vector_and_scanned_pages(client):
    uploaded = upload(client, FIXTURES / "pdf" / "sample_3pages.pdf").json()["source"]
    with SessionLocal() as db:
        pages = db.query(SourcePage).filter(SourcePage.source_id == uploaded["id"]).order_by(SourcePage.page_no).all()
        assert [page.page_no for page in pages] == [1, 2, 3]
        assert pages[0].has_text is True
        assert pages[1].has_text is True
        assert pages[2].has_text is False
        assert pages[2].is_scanned is True
        assert all(page.page_image_asset_id for page in pages)

        evidence = db.query(Evidence).filter(Evidence.source_id == uploaded["id"]).all()
        kinds = {item.kind for item in evidence}
        assert {"text_span", "page_image"} <= kinds
        page_images = [item for item in evidence if item.kind == "page_image"]
        assert {item.page_no for item in page_images} == {1, 2, 3}


def test_text_is_split_into_evidence_blocks(client):
    uploaded = upload(client, FIXTURES / "text" / "note.txt").json()["source"]
    with SessionLocal() as db:
        evidence = db.query(Evidence).filter(Evidence.source_id == uploaded["id"]).all()
    assert evidence
    assert all(item.kind == "text_span" for item in evidence)
    assert all(item.quote_text for item in evidence)
    assert [item.ordinal for item in evidence] == list(range(1, len(evidence) + 1))


def test_gbk_text_is_decoded(client):
    response = upload(client, FIXTURES / "text" / "note_gbk.txt")
    assert response.status_code == 201
    assert response.json()["source"]["kind"] == "text"