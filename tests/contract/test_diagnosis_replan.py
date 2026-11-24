from __future__ import annotations

import json
import time

from sqlalchemy import func, select

from app.domain import diagnosis
from app.domain.models import (
    DiagnosticAnswer,
    DiagnosticQuestion,
    DiagnosticRound,
    DiagnosticSession,
    GenerationJob,
    KnowledgeBase,
    KnowledgePoint,
    LearningEvidence,
    LearningPack,
    LearningPlan,
    LearningPlanVersion,
    SourceMaterial,
    now_iso,
)
from app.infra.db import SessionLocal
from app.infra.ids import new_id
from tests import stage4_helpers as helpers
from tests.stage4_helpers import wait_job


def _upload(client, tmp_path, name: str, text: str) -> dict:
    path = tmp_path / f"{name}_{time.time_ns()}.md"
    path.write_text(text, encoding="utf-8")
    return helpers.upload(client, path)


def _seed_confirmed_session(client, tmp_path) -> dict:
    source_a = _upload(
        client,
        tmp_path,
        "replan_a",
        "# 子网划分\n\n/26 的掩码是 255.255.255.192。\n\n# 路由基础\n\n路由器按路由表转发。\n",
    )
    source_b = _upload(
        client,
        tmp_path,
        "replan_b",
        "# TCP 拥塞控制\n\n慢启动和拥塞避免。\n\n# 快速重传\n\n三个重复 ACK 后立即重传。\n",
    )
    now = now_iso()
    suffix = str(time.time_ns())
    kb_id = f"kb_replan_{suffix}"
    old_plan_id = f"plan_replan_{suffix}"
    old_version_id = f"pv_replan_{suffix}"
    session_id = f"dgs_replan_{suffix}"
    old_job_id = f"job_replan_{suffix}"
    with SessionLocal() as db:
        db.add(
            KnowledgeBase(
                id=kb_id,
                name="重规划验收库",
                description="",
                created_at=now,
                updated_at=now,
            )
        )
        pseudo_points = []
        for source in (source_a, source_b):
            pseudo = KnowledgePoint(
                id=new_id("knowledge_point"),
                source_id=source["id"],
                display_name="全部学习",
                name_normalized="全部学习",
                status="active",
                created_at=now,
                updated_at=now,
            )
            db.add(pseudo)
            pseudo_points.append(pseudo)
        db.flush()
        modules = [
            {
                "module_id": "mod_legacy",
                "ordinal": 1,
                "title": "全部学习",
                "objective": "学完整门课。",
                "core_points": ["全部学习"],
                "knowledge_point_ids": [point.id for point in pseudo_points],
                "completion_standard": "完成全部内容。",
                "suggested_question_count": 2,
                "source_ids": [source_a["id"], source_b["id"]],
                "source_names": [source_a["display_name"], source_b["display_name"]],
                "page_start": None,
                "page_end": None,
            }
        ]
        items = [
            {
                "knowledge_point_id": point.id,
                "source_id": point.source_id,
                "display_name": "全部学习",
                "priority": "pending",
                "suggested_question_count": 1,
            }
            for point in pseudo_points
        ]
        db.add(
            LearningPlan(
                id=old_plan_id,
                title="旧的全部学习计划",
                status="confirmed",
                current_version_id=old_version_id,
                primary_source_id=source_a["id"],
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            LearningPlanVersion(
                id=old_version_id,
                plan_id=old_plan_id,
                status="confirmed",
                origin="suggested",
                adjustment_json="[]",
                sources_json=json.dumps(
                    [
                        {"source_id": source_a["id"], "page_start": None, "page_end": None},
                        {"source_id": source_b["id"], "page_start": None, "page_end": None},
                    ],
                    ensure_ascii=False,
                ),
                scope_json=json.dumps(
                    {"auto_plan": True, "learning_mode": "foundation", "goal": "全部学习"},
                    ensure_ascii=False,
                ),
                items_json=json.dumps(items, ensure_ascii=False),
                modules_json=json.dumps(modules, ensure_ascii=False),
                question_count=2,
                created_at=now,
                confirmed_at=now,
            )
        )
        db.add(
            DiagnosticSession(
                id=session_id,
                source_id=source_a["id"],
                target_goal="全部学习",
                status="confirmed",
                current_round=1,
                session_key=f"replan-{suffix}",
                pending_items_json=json.dumps(
                    {
                        "learned_point_ids": [],
                        "unlearned_point_ids": [],
                        "uncertain": [],
                        "question_count": None,
                        "learning_mode": "foundation",
                    },
                    ensure_ascii=False,
                ),
                plan_version_id=old_version_id,
                knowledge_base_id=kb_id,
                source_scope_json=json.dumps(
                    [
                        {"source_id": source_a["id"], "page_start": None, "page_end": None},
                        {"source_id": source_b["id"], "page_start": None, "page_end": None},
                    ],
                    ensure_ascii=False,
                ),
                created_at=now,
                updated_at=now,
                confirmed_at=now,
            )
        )
        db.flush()
        round_row = DiagnosticRound(
            id=new_id("diagnosis_round"),
            session_id=session_id,
            ordinal=1,
            intent="probe_question",
            status="active",
            created_at=now,
        )
        db.add(round_row)
        db.flush()
        question = DiagnosticQuestion(
            id=new_id("diagnosis_question"),
            session_id=session_id,
            round_id=round_row.id,
            ordinal=1,
            kind="diagnostic",
            stem="子网划分小题？",
            options_json=json.dumps(
                [{"key": "A", "text": "正确"}, {"key": "B", "text": "错误"}],
                ensure_ascii=False,
            ),
            answer_index=0,
            explanation="依据资料。",
            knowledge_point_id=pseudo_points[0].id,
            knowledge_point_name="全部学习",
            citation_json="[]",
            status="active",
            created_at=now,
        )
        db.add(question)
        db.flush()
        db.add(
            DiagnosticAnswer(
                id=new_id("diagnosis_answer"),
                session_id=session_id,
                question_id=question.id,
                selected_index=0,
                status="answered",
                is_correct=True,
                answer_key=f"replan-answer-{suffix}",
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        session = db.get(DiagnosticSession, session_id)
        source = db.get(SourceMaterial, source_a["id"])
        diagnosis._write_diagnostic_evidence_once(db, session, source)
        db.add(
            GenerationJob(
                id=old_job_id,
                idempotency_key=f"diagnostic:{session_id}",
                status="failed",
                stage="validate",
                stage_updated_at=now,
                request_json=json.dumps(
                    {"plan_version_id": old_version_id}, ensure_ascii=False
                ),
                input_hash="legacy-ambiguous",
                error_code="plan_allocation_mismatch",
                error_detail="分批合并后考点或模块题量与已确认计划不一致",
                created_at=now,
                updated_at=now,
                launch_state=None,
            )
        )
        db.commit()
        evidence_before = int(
            db.execute(select(func.count()).select_from(LearningEvidence)).scalar_one()
        )
    return {
        "session_id": session_id,
        "old_plan_id": old_plan_id,
        "old_version_id": old_version_id,
        "old_job_id": old_job_id,
        "kb_id": kb_id,
        "evidence_before": evidence_before,
    }


def test_replan_excludes_intent_points_and_keeps_evidence_once(client, tmp_path):
    seeded = _seed_confirmed_session(client, tmp_path)
    status = client.get(f"/api/diagnosis/sessions/{seeded['session_id']}").json()
    generation = status["generation"]
    assert generation["actions"] == ["replan"]
    assert generation["retry_available"] is False
    assert generation["error"]["code"] == "plan_allocation_mismatch"
    assert "重新制定学习计划" in generation["error"]["message"]
    job_status = client.get(f"/api/jobs/{seeded['old_job_id']}").json()
    assert job_status["actions"] == ["replan"]
    assert job_status["retry_available"] is False
    assert job_status["error"]["retryable"] is False

    response = client.post(
        f"/api/diagnosis/sessions/{seeded['session_id']}/replan",
        json={"goal": "全部学习", "question_count": None, "learning_mode": "foundation"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["status"] == "preview"
    new_version_id = body["version"]["id"]
    assert new_version_id != seeded["old_version_id"]
    modules = body["version"]["modules"]
    assert len(modules) >= 2
    for module in modules:
        assert "全部学习" not in (module.get("core_points") or [])
        assert module.get("source_names")
        assert module.get("source_scopes")
    item_names = {item["display_name"] for item in body["version"]["items"]}
    assert "全部学习" not in item_names
    assert {"子网划分", "路由基础", "TCP 拥塞控制", "快速重传"} <= item_names
    assert any(
        item.get("display_name") == "全部学习"
        for item in body.get("excluded_points") or []
    )

    with SessionLocal() as db:
        old_version = db.get(LearningPlanVersion, seeded["old_version_id"])
        old_plan = db.get(LearningPlan, seeded["old_plan_id"])
        old_job = db.get(GenerationJob, seeded["old_job_id"])
        assert old_version is not None and old_version.status == "confirmed"
        assert old_plan is not None
        assert old_job is not None and old_job.status == "failed"
        assert any(
            item.get("display_name") == "全部学习"
            for item in json.loads(old_version.items_json or "[]")
        )

    confirmed = client.post(
        f"/api/diagnosis/sessions/{seeded['session_id']}/confirm",
        json={"idempotency_key": f"replan-confirm-{time.time_ns()}"},
    )
    assert confirmed.status_code == 200, confirmed.text
    job = wait_job(client, confirmed.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    detail = client.get(f"/api/packs/{job['pack_id']}").json()
    assert detail["questions"]
    assert detail["modules"]

    with SessionLocal() as db:
        evidence_after = int(
            db.execute(select(func.count()).select_from(LearningEvidence)).scalar_one()
        )
        assert evidence_after == seeded["evidence_before"]
        containers = db.execute(
            select(LearningPack).where(
                LearningPack.job_id == f"diagnostic:{seeded['session_id']}"
            )
        ).scalars().all()
        assert len(containers) == 1
        old_job = db.get(GenerationJob, seeded["old_job_id"])
        assert old_job is not None and old_job.status == "failed"
        new_version = db.get(LearningPlanVersion, new_version_id)
        assert new_version is not None
        assert new_version.status == "confirmed"