from __future__ import annotations

import json

from sqlalchemy import select

from app.domain.generation import assemble_for_sources
from app.domain.models import Evidence, SourceMaterial
from app.domain.text_index import backfill_all
from app.infra.db import SessionLocal
from app.infra.model_client import ModelRequest, TextPayload, build_payload_for, encode_payload
from tests import stage4_helpers as helpers

PREFIX = (
    "第一部分：交换机根据目的 MAC 地址在同网段内转发帧，主机在同一广播域内直接通信。"
    "初学者容易把跨设备误认为跨网段，判断时应先看地址范围与默认网关。"
    "本段用于把后段知识点推到原文靠后的位置，包含空闲、缩进与中文标点。"
)
MIDDLE = (
    "第二部分：跨网段通信需要把数据交给默认网关，由路由器按路由表继续转发。"
    "路由器接口分别属于不同网段，静态路由和动态路由都可以提供下一跳。"
)
TAIL = (
    "第三部分：TCP 快速重传触发条件。接收端连续收到三个重复 ACK 时，"
    "发送端不等待重传计时器到期，立即重传丢失的报文段；"
    "三个重复 ACK 必须针对同一报文段，且接收窗口不得为零。"
)


def _sample_text() -> str:
    text = "\n\n".join([PREFIX * 3, MIDDLE * 2, TAIL])
    assert text.find("TCP 快速重传") > 350
    return text


def _rows(db, source_id: str):
    return db.execute(
        select(Evidence)
        .where(Evidence.source_id == source_id, Evidence.kind == "text_span")
        .order_by(Evidence.ordinal)
    ).scalars().all()


def test_new_text_source_keeps_full_body_and_char_offsets(client, tmp_path):
    text = _sample_text()
    path = tmp_path / "three_topics.txt"
    path.write_bytes(text.encode("utf-8"))
    source = helpers.upload(client, path)

    with SessionLocal() as db:
        rows = _rows(db, source["id"])
        with_tail = [row for row in rows if row.body_text and "三个重复 ACK" in row.body_text]
        assert with_tail, [row.quote_text for row in rows]
        for row in rows:
            chars = json.loads(row.locator_json)["chars"]
            assert row.body_text == text[chars[0] : chars[1]]
            assert row.quote_text == row.body_text[:200]

        assembly = assemble_for_sources(
            db,
            [{"source_id": source["id"], "page_start": None, "page_end": None}],
            load_images=False,
            text_limit=3,
            image_limit=0,
            balanced=True,
            query_terms=["TCP 快速重传", "三个重复 ACK"],
        )
        sent = "\n".join(item.description for item in assembly.text_items)
        assert "三个重复 ACK" in sent
        assert "接收端连续收到三个重复 ACK" in sent

        request = ModelRequest(
            system_prompt="system",
            task_text="task",
            text_items=assembly.text_items,
            images=assembly.images,
        )
        payload = encode_payload(build_payload_for(request, "test-model"))
        assert "三个重复 ACK" in payload.decode("utf-8")


def test_legacy_text_source_backfill_restores_searchable_body(client, tmp_path):
    text = _sample_text()
    path = tmp_path / "legacy.txt"
    path.write_bytes(text.encode("utf-8"))
    source = helpers.upload(client, path)

    with SessionLocal() as db:
        material = db.get(SourceMaterial, source["id"])
        material.text_index_version = 0
        for row in _rows(db, source["id"]):
            row.body_text = None
            row.quote_text = (row.quote_text or "")[:40]
        db.commit()
        results = backfill_all(db)
        assert any(item.get("source_id") == source["id"] and item.get("ok") for item in results)
        rows = _rows(db, source["id"])
        assert any(row.body_text and "三个重复 ACK" in row.body_text for row in rows)
        for row in rows:
            chars = json.loads(row.locator_json)["chars"]
            assert row.body_text == text[chars[0] : chars[1]]
        material = db.get(SourceMaterial, source["id"])
        assert material.text_index_version >= 1

        # 可重复执行：再次补索引不报错、不产生重复证据
        before = len(rows)
        backfill_all(db)
        assert len(_rows(db, source["id"])) == before


def test_missing_legacy_text_file_reports_retry_without_blocking_others(client, tmp_path):
    good_path = tmp_path / "good.txt"
    good_path.write_bytes(_sample_text().encode("utf-8"))
    good = helpers.upload(client, good_path)
    missing_path = tmp_path / "missing.txt"
    missing_path.write_bytes("旧资料正文".encode("utf-8"))
    missing = helpers.upload(client, missing_path)

    with SessionLocal() as db:
        material = db.get(SourceMaterial, missing["id"])
        material.stored_path = "sources/not-there/original.txt"
        material.text_index_version = 0
        good_material = db.get(SourceMaterial, good["id"])
        good_material.text_index_version = 0
        db.commit()
        results = backfill_all(db)
        by_id = {item.get("source_id"): item for item in results}
        assert by_id[missing["id"]].get("ok") is False
        assert by_id[good["id"]].get("ok") is True
        material = db.get(SourceMaterial, missing["id"])
        assert material.text_index_error


def test_long_markdown_tail_topic_is_retrieved_before_prefix(client, tmp_path):
    paragraphs = [
        f"第{index}段：常规章节内容，介绍概念、判断条件与应用步骤。" * 3
        for index in range(1, 31)
    ]
    paragraphs.append(
        "末章：BGP 路由反射器的簇 ID 规则。路由反射器在簇内反射路由时，"
        "不修改 AS_PATH，但必须保留 ORIGINATOR_ID 与 CLUSTER_LIST；"
        "簇 ID 用于防止同一簇内的路由环路。"
    )
    text = "\n\n".join(paragraphs)
    assert text.find("簇 ID") > 2000
    path = tmp_path / "long.md"
    path.write_bytes(text.encode("utf-8"))
    source = helpers.upload(client, path)

    with SessionLocal() as db:
        assembly = assemble_for_sources(
            db,
            [{"source_id": source["id"], "page_start": None, "page_end": None}],
            load_images=False,
            text_limit=2,
            image_limit=0,
            balanced=True,
            query_terms=["BGP 路由反射器", "CLUSTER_LIST", "簇 ID"],
        )
        sent = "\n".join(item.description for item in assembly.text_items)
        assert "CLUSTER_LIST" in sent
        assert "簇 ID" in sent