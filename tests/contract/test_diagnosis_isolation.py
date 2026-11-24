from __future__ import annotations

import time

from sqlalchemy import select, update

from app.domain import plans
from app.domain.models import Citation, LearningEvidence, LearningPack
from app.domain.schemas import PlanPointItem, PlanSourceItem, SuggestPlanRequest
from app.infra.db import SessionLocal
from tests import stage4_helpers as helpers


def _submit(client, pack):
    answers = []
    with SessionLocal() as db:
        for question in pack["questions"]:
            row = db.get(__import__("app.domain.models", fromlist=["Question"]).Question, question["id"])
            answers.append({"question_id": question["id"], "selected_index": row.answer_index})
    return client.post(
        f"/api/packs/{pack['id']}/submit",
        json={"idempotency_key": f"page-{time.time_ns()}", "answers": answers},
    )


def test_page_fingerprint_excludes_unverifiable_pages(client, tmp_path):
    source, pack = helpers.generate_pack(client, tmp_path, color=(51, 61, 71), count=3)
    assert _submit(client, pack).status_code == 200
    with SessionLocal() as db:
        evidence = db.execute(
            select(LearningEvidence).where(LearningEvidence.source_id == source["id"])
        ).scalars().all()
        assert len(evidence) == 3
        pages = [2, None, 99]
        for row, page in zip(evidence, pages):
            citation = db.get(Citation, row.citation_id) if row.citation_id else None
            if citation is None:
                citation = Citation(
                    id=__import__("app.infra.ids", fromlist=["new_id"]).new_id("citation"),
                    pack_id=row.pack_id,
                    owner_type="question",
                    owner_id=row.question_id,
                    evidence_id="offline",
                    source_id=source["id"],
                    page_no=page,
                    locator_json="{}",
                    quote_snapshot="",
                    claim_text="",
                    local_ref="F1",
                    created_at="2026-11-20T00:00:00Z",
                )
                db.add(citation)
                db.flush()
                row.citation_id = citation.id
            else:
                citation.page_no = page
        db.commit()
        point = db.execute(
            select(LearningEvidence).where(LearningEvidence.source_id == source["id"])
        ).scalars().first()
        payload = SuggestPlanRequest(
            title="按页复习",
            sources=[PlanSourceItem(source_id=source["id"], page_start=1, page_end=2)],
            goal="只看第 1–2 页",
            question_count=3,
            page_start=1,
            page_end=2,
            points=[
                PlanPointItem(
                    knowledge_point_id=point.knowledge_point_id,
                    display_name="按页考点",
                    suggested_question_count=3,
                )
            ],
        )
        result = plans.suggest_next_plan(db, source["id"], payload)
    assert result["no_page_evidence_count"] == 2


def test_v4_fresh_schema_and_legacy_pack_visible(client, tmp_path):
    from app.domain.models import DiagnosticSession, DiagnosticQuestion, DiagnosticAnswer, DiagnosticRound
    from app.infra.migrations import TARGET_SCHEMA_VERSION

    assert TARGET_SCHEMA_VERSION == "11"
    source, pack = helpers.generate_pack(client, tmp_path, color=(52, 62, 72), count=3)
    assert client.get(f"/api/packs/{pack['id']}").status_code == 200
    listed = client.get("/api/packs").json()["items"]
    assert any(item["id"] == pack["id"] for item in listed)
    with SessionLocal() as db:
        assert db.get(LearningPack, pack["id"]).source_kind == "real"
        tables = {
            row[0]
            for row in db.connection().exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"diagnostic_session", "diagnostic_round", "diagnostic_question", "diagnostic_answer"} <= tables
        columns = {
            row[1] for row in db.connection().exec_driver_sql("PRAGMA table_info(learning_evidence)")
        }
        assert "diagnostic_answer_id" in columns
        assert db.get(LearningPack, pack["id"]).source_kind == "real"
        assert db.execute(
            select(DiagnosticSession).where(DiagnosticSession.id == "not-created")
        ).scalars().all() == []

def test_offline_diagnosis_branches_are_isolated(client):
    from app.domain.models import LearningEvidence, LearningPlan

    with SessionLocal() as db:
        before_evidence = len(db.execute(select(LearningEvidence)).scalars().all())
        before_plans = len(db.execute(select(LearningPlan)).scalars().all())
    sample = client.get("/api/offline/diagnosis/sample")
    assert sample.status_code == 200
    assert sample.json()["is_offline_sample"] is True
    branch_a = client.post("/api/offline/diagnosis/answer", json={"branch": "a"})
    branch_b = client.post("/api/offline/diagnosis/answer", json={"branch": "b"})
    assert branch_a.status_code == 200 and branch_b.status_code == 200
    assert branch_a.json()["plan"]["items"] != branch_b.json()["plan"]["items"]
    assert "判断同网段" in branch_b.json()["plan"]["excluded"]
    assert client.post("/api/offline/diagnosis/reset").status_code == 200
    with SessionLocal() as db:
        after_evidence = len(db.execute(select(LearningEvidence)).scalars().all())
        after_plans = len(db.execute(select(LearningPlan)).scalars().all())
    assert after_evidence == before_evidence
    assert after_plans == before_plans