from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

from app.domain import assessment as assessment_service
from app.domain.models import (
    Citation,
    Evidence,
    KnowledgePoint,
    LearningEvidence,
    LearningPack,
    LearningPlanVersion,
    PackPlanVersion,
    PackSource,
    Question,
    QuestionKnowledgePoint,
    ScheduleTask,
    SourceMaterial,
    now_iso,
)
from app.infra.db import SessionLocal
from app.infra.ids import new_id
from app.infra.model_client import FakeModelClient
from tests import stage4_helpers as helpers


class PerSourceClient:
    def __init__(self, failing_source_id: str | None = None, out_of_scope_evidence: str | None = None):
        self.failing_source_id = failing_source_id
        self.out_of_scope_evidence = out_of_scope_evidence
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        raw = request.task_text.split("【测评输入】\n", 1)[1]
        data, _ = json.JSONDecoder().raw_decode(raw.strip())
        if data["source"]["source_id"] != self.failing_source_id:
            return FakeModelClient().generate(request)
        point_id = data["points"][0]["knowledge_point_id"]
        return SimpleNamespace(
            content_text=json.dumps(
                {
                    "findings": [
                        {
                            "knowledge_point_id": point_id,
                            "kind": "observation",
                            "judgement": "测试用越界证据。",
                            "evidence_ids": [self.out_of_scope_evidence or "learning_evidence_missing"],
                        }
                    ],
                    "route_steps": [],
                    "unknowns": [],
                },
                ensure_ascii=False,
            ),
            finish_reason="stop",
            usage={},
        )


def _module():
    return [
        {
            "module_id": "mod_cross",
            "title": "跨资料模块",
            "knowledge_point_ids": ["kp_cross_a", "kp_cross_b"],
            "core_points": ["资料甲考点", "资料乙考点"],
            "suggested_question_count": 4,
            "source_names": ["资料甲", "资料乙"],
        }
    ]


def _create_plan(client, source_a, source_b, name):
    response = client.post(
        "/api/plans",
        json={
            "title": name,
            "sources": [
                {"source_id": source_a["id"], "page_start": None, "page_end": None},
                {"source_id": source_b["id"], "page_start": None, "page_end": None},
            ],
            "goal": "跨资料测评",
            "points": [],
            "question_count": 4,
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    version_id = plan["current_version_id"]
    with SessionLocal() as db:
        version = db.get(LearningPlanVersion, version_id)
        version.modules_json = json.dumps(_module(), ensure_ascii=False)
        version.question_count = 4
        db.commit()
    confirmed = client.post(f"/api/plans/{plan['id']}/versions/{version_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return plan["id"], version_id


def _prepare_sources(client, tmp_path, colors):
    sources = []
    for index, color in enumerate(colors):
        sources.append(
            helpers.upload(
                client, helpers.make_png(tmp_path / f"cross_{index}_{time.time_ns()}.png", color)
            )
        )
    return sources


def _evidence_row(db, source_id):
    row = db.execute(
        select(Evidence).where(Evidence.source_id == source_id).order_by(Evidence.ordinal)
    ).scalars().first()
    assert row is not None
    return row


def _seed_pack(db, version_id, source_a, source_b, layout):
    version = db.get(LearningPlanVersion, version_id)
    pack = LearningPack(
        id=new_id("pack"),
        display_title="跨资料学习内容",
        source_kind="real",
        difficulty="consolidate",
        question_count=len(layout),
        job_id=f"job_cross_{time.time_ns()}",
        handout_word_count=0,
        model_name="test",
        prompt_version="test",
        created_at=now_iso(),
    )
    db.add(pack)
    db.flush()
    source_map = {"a": source_a, "b": source_b}
    evidence_map = {
        "a": _evidence_row(db, source_a["id"]),
        "b": _evidence_row(db, source_b["id"]),
    }
    point_ids = {"a": f"kp_{pack.id}_a", "b": f"kp_{pack.id}_b"}
    point_names = {"a": "资料甲考点", "b": "资料乙考点"}
    for key in ("a", "b"):
        db.add(
            KnowledgePoint(
                id=point_ids[key],
                source_id=source_map[key]["id"],
                display_name=point_names[key],
                name_normalized=f"{point_names[key]}-{time.time_ns()}",
                status="active",
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )
    for key in ("a", "b"):
        source = source_map[key]
        stored = db.get(SourceMaterial, source["id"])
        assert stored is not None
        db.add(
            PackSource(
                pack_id=pack.id,
                source_id=source["id"],
                page_start=None,
                page_end=None,
                page_count_used=None,
                source_name_snapshot=stored.display_name,
                source_sha256_snapshot=stored.sha256,
                source_kind_snapshot=stored.kind,
            )
        )
    db.flush()
    questions = []
    for ordinal, (key, stem) in enumerate(layout, start=1):
        source = source_map[key]
        question = Question(
            id=new_id("question"),
            pack_id=pack.id,
            ordinal=ordinal,
            stem=stem,
            options_json=json.dumps(
                [
                    {"key": "A", "text": "正确选项"},
                    {"key": "B", "text": "干扰项"},
                    {"key": "C", "text": "干扰项"},
                    {"key": "D", "text": "干扰项"},
                ],
                ensure_ascii=False,
            ),
            answer_index=0,
            explanation="测试解析",
            knowledge_point=point_names[key],
            module_id="mod_cross",
            difficulty_tag="consolidate",
            is_image_based=False,
            stem_hash=hashlib.sha256(f"{pack.id}-{stem}".encode("utf-8")).hexdigest(),
            created_at=now_iso(),
        )
        db.add(question)
        db.flush()
        questions.append(question)
        db.add(
            QuestionKnowledgePoint(
                question_id=question.id,
                knowledge_point_id=point_ids[key],
                role="primary",
                reliability="confirmed",
                created_at=now_iso(),
            )
        )
        evidence = evidence_map[key]
        db.add(
            Citation(
                id=new_id("citation"),
                pack_id=pack.id,
                owner_type="question",
                owner_id=question.id,
                evidence_id=evidence.id,
                source_id=source["id"],
                page_no=evidence.page_no,
                locator_json=json.dumps({"kind": "test"}, ensure_ascii=False),
                quote_snapshot="测试引用",
                claim_text=point_names[key],
                local_ref="T1",
                created_at=now_iso(),
            )
        )
    db.add(
        PackPlanVersion(
            pack_id=pack.id,
            plan_version_id=version_id,
            adopted_at=now_iso(),
        )
    )
    db.commit()
    return version.plan_id, pack.id, questions


def _schedule_practice_task(client, plan_id, version_id, pack_id, questions):
    saved = client.post(
        f"/api/plans/{plan_id}/versions/{version_id}/schedule",
        json={"start_date": "2030-08-01", "duration_days": 14, "daily_minutes": 30},
    )
    assert saved.status_code == 200, saved.text
    with SessionLocal() as db:
        task = db.execute(
            select(ScheduleTask).where(
                ScheduleTask.plan_version_id == version_id,
                ScheduleTask.task_type == "practice",
            )
        ).scalars().first()
    assert task is not None
    scoped = client.get(f"/api/packs/{pack_id}?task_id={task.id}")
    assert scoped.status_code == 200, scoped.text
    return task, scoped.json()


def _submit(client, pack_id, task_id, questions):
    return client.post(
        f"/api/packs/{pack_id}/submit",
        json={
            "idempotency_key": f"cross-submit-{time.time_ns()}",
            "task_id": task_id,
            "answers": [
                {"question_id": question.id, "selected_index": 0}
                for question in questions
            ],
        },
    ).json()


def test_cross_source_attempt_is_assessed_per_source(client, tmp_path):
    source_a, source_b = _prepare_sources(client, tmp_path, [(32, 132, 72), (40, 140, 80)])
    plan_id, version_id = _create_plan(client, source_a, source_b, "跨资料测评计划")
    layout = [("a", "资料甲 第 1 题"), ("a", "资料甲 第 2 题"), ("b", "资料乙 第 1 题"), ("b", "资料乙 第 2 题")]
    with SessionLocal() as db:
        plan_id, pack_id, questions = _seed_pack(db, version_id, source_a, source_b, layout)
    task, page = _schedule_practice_task(client, plan_id, version_id, pack_id, questions)
    assert page["question_count"] == 4
    submitted = _submit(client, pack_id, task.id, questions)

    with SessionLocal() as db:
        rows = db.execute(
            select(LearningEvidence).where(
                LearningEvidence.attempt_id == submitted["attempt_id"]
            )
        ).scalars().all()
        assert len(rows) == 4
        assert {row.source_id for row in rows} == {source_a["id"], source_b["id"]}

    result = client.post(
        f"/api/packs/{pack_id}/assessment",
        json={"attempt_id": submitted["attempt_id"]},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["multi_source"] is True
    assert body["status"] == "ready"
    assert body["coverage"]["total_questions"] == 4
    assert body["coverage"]["covered_questions"] == 4
    assert body["coverage"]["source_count"] == 2
    sources = {item["source_id"]: item for item in body["sources"]}
    assert set(sources) == {source_a["id"], source_b["id"]}
    assert sources[source_a["id"]]["current_evidence_count"] == 2
    assert sources[source_b["id"]]["current_evidence_count"] == 2
    assert sources[source_a["id"]]["covered_questions"] == 2
    assert sources[source_b["id"]]["covered_questions"] == 2
    assert body["label"] == "2 份资料 · 2 份已完成"

    with SessionLocal() as db:
        snapshots = assessment_service.snapshots_for_attempt(db, submitted["attempt_id"])
        assert len(snapshots) == 2
        by_source = {row.source_id: row for row in snapshots}
        for source_id in (source_a["id"], source_b["id"]):
            stored = json.loads(by_source[source_id].evidence_ids_json)
            assert len(stored) == 2

    again = client.post(
        f"/api/packs/{pack_id}/assessment",
        json={"attempt_id": submitted["attempt_id"]},
    ).json()
    assert {item["snapshot_id"] for item in again["sources"]} == {
        item["snapshot_id"] for item in body["sources"]
    }

    profile_a = client.get(f"/api/learner-profile?source_id={source_a['id']}").json()
    profile_b = client.get(f"/api/learner-profile?source_id={source_b['id']}").json()
    assert profile_a["items"]
    assert profile_b["items"]


def test_group_entirely_from_non_primary_source_is_assessed_from_that_source(client, tmp_path):
    source_a, source_b = _prepare_sources(client, tmp_path, [(50, 150, 90), (60, 160, 100)])
    plan_id, version_id = _create_plan(client, source_a, source_b, "非首份资料计划")
    layout = [("b", "资料乙 第 1 题"), ("b", "资料乙 第 2 题")]
    with SessionLocal() as db:
        plan_id, pack_id, questions = _seed_pack(db, version_id, source_a, source_b, layout)
    task, _ = _schedule_practice_task(client, plan_id, version_id, pack_id, questions)
    submitted = _submit(client, pack_id, task.id, questions)

    with SessionLocal() as db:
        rows = db.execute(
            select(LearningEvidence).where(
                LearningEvidence.attempt_id == submitted["attempt_id"]
            )
        ).scalars().all()
        assert {row.source_id for row in rows} == {source_b["id"]}

    result = client.post(
        f"/api/packs/{pack_id}/assessment",
        json={"attempt_id": submitted["attempt_id"]},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["source_id"] == source_b["id"]
    assert body["multi_source"] is False
    assert {item["source_id"] for item in body["sources"]} == {source_b["id"]}
    assert body["sources"][0]["current_evidence_count"] == 2
    covered = [item for item in body["coverage"]["sources"] if item["source_id"] == source_b["id"]]
    assert covered and covered[0]["covered_questions"] == 2

    with SessionLocal() as db:
        snapshots = assessment_service.snapshots_for_attempt(db, submitted["attempt_id"])
        assert len(snapshots) == 1
        assert snapshots[0].source_id == source_b["id"]


def test_partial_failure_keeps_other_source_and_retries_only_missing(
    client, tmp_path, monkeypatch
):
    source_a, source_b = _prepare_sources(client, tmp_path, [(70, 170, 110), (80, 180, 120)])
    plan_id, version_id = _create_plan(client, source_a, source_b, "部分失败计划")
    layout = [("a", "资料甲 第 1 题"), ("a", "资料甲 第 2 题"), ("b", "资料乙 第 1 题"), ("b", "资料乙 第 2 题")]
    with SessionLocal() as db:
        plan_id, pack_id, questions = _seed_pack(db, version_id, source_a, source_b, layout)
    task, _ = _schedule_practice_task(client, plan_id, version_id, pack_id, questions)
    submitted = _submit(client, pack_id, task.id, questions)

    failing = PerSourceClient(failing_source_id=source_a["id"])
    monkeypatch.setattr(assessment_service, "get_model_client", lambda: failing)
    first = client.post(
        f"/api/packs/{pack_id}/assessment",
        json={"attempt_id": submitted["attempt_id"]},
    ).json()
    assert first["status"] == "partial"
    sources = {item["source_id"]: item for item in first["sources"]}
    assert sources[source_a["id"]]["status"] == "failed"
    assert sources[source_b["id"]]["status"] == "ready"
    assert first["retry_sources"] == [source_a["id"]]
    ready_b = sources[source_b["id"]]["snapshot_id"]

    brief = client.get(
        f"/api/schedule/brief?plan_id={plan_id}&version_id={version_id}&date={date.today()}"
    )
    assert brief.status_code == 200, brief.text
    brief_body = brief.json()
    assert brief_body["practice"]["total_questions"] == 4
    assert brief_body["practice"]["answered"] == 4
    assert brief_body["practice"]["correct"] == 4
    assert "4 题里答对了 4 题" in brief_body["summary"]

    monkeypatch.setattr(assessment_service, "get_model_client", lambda: FakeModelClient())
    retried = client.post(
        f"/api/packs/{pack_id}/assessment",
        json={"attempt_id": submitted["attempt_id"], "source_id": source_a["id"]},
    ).json()
    sources = {item["source_id"]: item for item in retried["sources"]}
    assert sources[source_a["id"]]["status"] == "ready"
    assert sources[source_b["id"]]["status"] == "ready"
    assert sources[source_b["id"]]["snapshot_id"] == ready_b
    assert retried["status"] == "ready"


def test_uncovered_questions_stay_in_score_and_are_marked_pending(client, tmp_path):
    source_a, source_b = _prepare_sources(client, tmp_path, [(90, 190, 130), (100, 200, 140)])
    plan_id, version_id = _create_plan(client, source_a, source_b, "缺证据计划")
    layout = [("a", "资料甲 第 1 题"), ("b", "资料乙 第 1 题"), ("b", "资料乙 第 2 题")]
    with SessionLocal() as db:
        plan_id, pack_id, questions = _seed_pack(db, version_id, source_a, source_b, layout)
    task, _ = _schedule_practice_task(client, plan_id, version_id, pack_id, questions)
    submitted = _submit(client, pack_id, task.id, questions)

    with SessionLocal() as db:
        broken = db.execute(
            select(LearningEvidence).where(
                LearningEvidence.attempt_id == submitted["attempt_id"],
                LearningEvidence.source_id == source_a["id"],
            )
        ).scalars().first()
        broken.reliability = "pending"
        db.commit()

    body = client.post(
        f"/api/packs/{pack_id}/assessment",
        json={"attempt_id": submitted["attempt_id"]},
    ).json()
    assert body["coverage"]["total_questions"] == 3
    assert body["coverage"]["covered_questions"] == 2
    assert body["coverage"]["uncovered_questions"] == 1
    assert body["coverage"]["pending_verification"][0]["reason"] == "缺少可核对的作答证据"
    assert {item["source_id"] for item in body["sources"]} == {source_b["id"]}