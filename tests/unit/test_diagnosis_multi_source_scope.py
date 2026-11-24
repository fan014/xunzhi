from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.domain.diagnosis import (
    DiagnosisError,
    DiagnosticRef,
    _hydrate_frozen,
    _session_source_ids,
    _validate_refs,
)
from app.domain.models import DiagnosticSession, Evidence
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def _session() -> DiagnosticSession:
    return DiagnosticSession(
        id="dx_unit_scope",
        source_id="src_a",
        source_scope_json=json.dumps(
            [
                {"source_id": "src_a", "page_start": None, "page_end": None},
                {"source_id": "src_b", "page_start": 10, "page_end": 20},
            ]
        ),
    )


def _ref(source_id: str, ref: str = "T1") -> DiagnosticRef:
    return DiagnosticRef(
        ref=ref,
        evidence_id="ev_test",
        kind="text_span",
        page_no=11,
        quote="交换机按 MAC 转发",
        locator={"chars": [0, 4]},
        source_id=source_id,
        content_scope="text",
        origin="evidence",
        citation_id=None,
        verifiable=True,
    )


def test_session_source_ids_include_every_scoped_source():
    assert _session_source_ids(_session()) == {"src_a", "src_b"}


def test_validate_refs_accepts_sources_inside_knowledge_base_scope():
    resolved = _validate_refs(["T1"], {"T1": _ref("src_b")}, _session())
    assert [item.source_id for item in resolved] == ["src_b"]


def test_validate_refs_rejects_source_outside_scope():
    with pytest.raises(DiagnosisError) as excinfo:
        _validate_refs(["T1"], {"T1": _ref("src_c")}, _session())
    assert excinfo.value.code == "diagnostic_citation_out_of_scope"


def test_hydrate_frozen_keeps_each_evidence_own_source(client, tmp_path):
    note_a = tmp_path / "scope_a.txt"
    note_a.write_text("A 资料：同网段由交换机转发。", encoding="utf-8")
    note_b = tmp_path / "scope_b.txt"
    note_b.write_text("B 资料：跨网段交给默认网关。", encoding="utf-8")
    source_a = helpers.upload(client, note_a)
    source_b = helpers.upload(client, note_b)

    with SessionLocal() as db:
        evidence_ids = {}
        for key, source in (("a", source_a), ("b", source_b)):
            evidence_ids[key] = db.execute(
                select(Evidence.id)
                .where(Evidence.source_id == source["id"], Evidence.kind == "text_span")
                .order_by(Evidence.ordinal)
            ).scalars().first()
        session = DiagnosticSession(
            id="dx_unit_hydrate",
            source_id=source_a["id"],
            source_scope_json=json.dumps(
                [
                    {"source_id": source_a["id"], "page_start": None, "page_end": None},
                    {"source_id": source_b["id"], "page_start": None, "page_end": None},
                ]
            ),
        )
        frozen = {
            "T1": {"evidence_id": evidence_ids["a"], "origin": "evidence"},
            "T2": {"evidence_id": evidence_ids["b"], "origin": "evidence"},
        }
        refs = _hydrate_frozen(db, session, frozen)

    assert {item.ref for item in refs} == {"T1", "T2"}
    assert {item.source_id for item in refs} == {source_a["id"], source_b["id"]}