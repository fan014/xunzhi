from __future__ import annotations

import time

import pymupdf


def _wait_kb_ready(client, kb_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = client.get(f"/api/knowledge-bases/{kb_id}").json()
        states = {item["status"] for item in last.get("items") or []}
        if states and states <= {"ready", "failed"}:
            return last
        time.sleep(0.2)
    raise AssertionError(f"knowledge base did not finish: {last}")


def _toc_pdf(path) -> None:
    doc = pymupdf.open()
    for page_no in range(1, 7):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"第 {page_no} 页正文：链路层与网络层内容。", fontsize=11)
    doc.set_toc([[1, "数据链路层", 1], [1, "网络层", 4]])
    doc.save(path)
    doc.close()


def _create_kb(client, name: str) -> dict:
    response = client.post("/api/knowledge-bases", json={"name": name, "description": ""})
    assert response.status_code == 201, response.text
    return response.json()


def _upload_to_kb(client, kb_id: str, path, mime: str) -> dict:
    with open(path, "rb") as fh:
        response = client.post(
            f"/api/knowledge-bases/{kb_id}/items/upload",
            files=[("files", (path.name, fh, mime))],
        )
    assert response.status_code == 202, response.text
    return response.json()


def test_pdf_toc_becomes_selectable_chapters(client, tmp_path):
    pdf = tmp_path / "chapters.pdf"
    _toc_pdf(pdf)
    kb = _create_kb(client, "章节库")
    _upload_to_kb(client, kb["id"], pdf, "application/pdf")
    detail = _wait_kb_ready(client, kb["id"])

    item = detail["items"][0]
    chapters = item["chapters"]
    assert [chapter["title"] for chapter in chapters] == ["数据链路层", "网络层"]
    assert chapters[0]["page_start"] == 1 and chapters[0]["page_end"] == 3
    assert chapters[1]["page_start"] == 4 and chapters[1]["page_end"] == 6
    assert chapters[0]["label"] == "数据链路层 · 第 1–3 页"
    assert all(chapter["origin"] == "toc" for chapter in chapters)


def test_pdf_without_toc_falls_back_to_page_blocks(client, sample_pdf):
    kb = _create_kb(client, "无目录库")
    _upload_to_kb(client, kb["id"], sample_pdf, "application/pdf")
    detail = _wait_kb_ready(client, kb["id"])

    chapters = detail["items"][0]["chapters"]
    assert chapters
    assert chapters[0]["page_start"] == 1
    assert chapters[-1]["page_end"] == detail["items"][0]["page_count"]
    assert all(chapter["origin"] == "fixed" for chapter in chapters)
    assert chapters[0]["label"].startswith("第 1")


def test_non_pdf_items_have_no_chapters(client, tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("第一章 交换机转发\n\n同网段不经过路由器。", encoding="utf-8")
    kb = _create_kb(client, "文本库")
    _upload_to_kb(client, kb["id"], note, "text/plain")
    detail = _wait_kb_ready(client, kb["id"])
    assert detail["items"][0]["chapters"] == []