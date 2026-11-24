from __future__ import annotations

import time
from types import SimpleNamespace

from sqlalchemy import select

from app.domain import diagnosis
from app.domain.diagnosis import (
    _chapter_topics,
    _prepare_source_points,
    _source_topics,
    is_meta_point,
)
from app.domain.models import Evidence, KnowledgePoint, now_iso
from app.infra.db import SessionLocal
from app.infra.ids import new_id
from tests import stage4_helpers as helpers


def test_learning_intent_markers_are_not_points():
    for name in ("全部学习", "从头学", "学完整门课", "整门课", "全部内容"):
        assert is_meta_point(name), name
    assert is_meta_point("子网划分") is False


def test_legacy_intent_point_is_excluded_and_real_chapter_topics_are_used(client, tmp_path):
    note = tmp_path / f"intent_{time.time_ns()}.md"
    note.write_text(
        "# 子网划分\n\n/26 的掩码是 255.255.255.192。\n\n"
        "# 路由基础\n\n路由器按路由表转发分组。\n",
        encoding="utf-8",
    )
    source = helpers.upload(client, note)
    with SessionLocal() as db:
        db.add(
            KnowledgePoint(
                id=new_id("knowledge_point"),
                source_id=source["id"],
                display_name="全部学习",
                name_normalized="全部学习",
                status="active",
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
        db.commit()
        points = _prepare_source_points(db, source["id"], "全部学习")
        names = {point.display_name for point in points}
        assert "全部学习" not in names
        assert {"子网划分", "路由基础"} <= names
        stale = db.execute(
            select(KnowledgePoint).where(
                KnowledgePoint.source_id == source["id"],
                KnowledgePoint.display_name == "全部学习",
            )
        ).scalars().first()
        assert stale is not None
        assert stale.status == "candidate"
        db.commit()


def test_english_and_numbered_chapter_titles_become_topics(monkeypatch):
    chapters = [
        {
            "title": "Chapter 2: Subnetting",
            "page_start": 10,
            "page_end": 20,
            "origin": "toc",
        },
        {
            "title": "3.4 Congestion Control",
            "page_start": 21,
            "page_end": 30,
            "origin": "toc",
        },
        {
            "title": "Chapter 3",
            "page_start": 31,
            "page_end": 40,
            "origin": "toc",
        },
    ]
    monkeypatch.setattr(
        diagnosis, "chapters_for_source", lambda db, source, page_count: chapters
    )
    source = SimpleNamespace(id="src_x", page_count=100, kind="pdf", status="active")
    topics = _chapter_topics(None, source, None, None)
    names = [topic["display_name"] for topic in topics]
    assert names == ["Subnetting", "Congestion Control"]
    assert topics[0]["page_start"] == 10
    assert topics[1]["page_end"] == 30


def test_page_range_only_uses_that_range_evidence(client, sample_pdf, monkeypatch):
    source = helpers.upload(client, sample_pdf)
    monkeypatch.setattr(diagnosis, "chapters_for_source", lambda db, source, page_count: [])
    with SessionLocal() as db:
        db.query(Evidence).filter(Evidence.source_id == source["id"]).delete()
        db.add_all(
            [
                Evidence(
                    id=new_id("evidence"),
                    source_id=source["id"],
                    kind="text_span",
                    page_no=page_no,
                    ordinal=1,
                    locator_json="{}",
                    quote_text=text,
                    body_text=text,
                    confidence="high",
                    status="active",
                )
                for page_no, text in (
                    (1, "1.1 基础概念\n正文"),
                    (2, "2.3 TCP Congestion Control\n正文"),
                    (3, "3.1 其他内容\n正文"),
                )
            ]
        )
        db.commit()
        topics = _source_topics(db, source["id"], 2, 2)
    assert [topic["display_name"] for topic in topics] == ["TCP Congestion Control"]
    assert topics[0]["page_start"] == 2
    assert topics[0]["page_end"] == 2