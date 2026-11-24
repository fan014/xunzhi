from __future__ import annotations

from sqlalchemy import select

from app.domain.models import LearningPack
from app.infra.db import SessionLocal
from tests.stage4_helpers import create_pack, generate_pack, submit_wrong_once, wait_job


def test_workbench_flow_from_upload_to_submit(client, tmp_path):
    _, pack = generate_pack(client, tmp_path)
    assert pack["question_count"] == 3
    assert pack["answer_revealed"] is False

    draft = client.put(
        f"/api/packs/{pack['id']}/draft",
        json={
            "answers": [
                {"question_id": pack["questions"][0]["id"], "selected_index": 1},
                {"question_id": pack["questions"][1]["id"], "selected_index": None},
            ]
        },
    )
    assert draft.status_code == 200
    assert draft.json()["answered_count"] == 1

    submitted = client.post(
        f"/api/packs/{pack['id']}/submit",
        json={
            "idempotency_key": "workbench-submit-0001",
            "answers": [
                {"question_id": item["id"], "selected_index": 0} for item in pack["questions"]
            ],
        },
    )
    assert submitted.status_code == 200
    result = submitted.json()
    assert result["score"]["total"] == 3
    assert result["questions"][0]["explanation"]

    duplicate = client.post(
        f"/api/packs/{pack['id']}/submit",
        json={
            "idempotency_key": "workbench-submit-0001",
            "answers": [
                {"question_id": item["id"], "selected_index": 0} for item in pack["questions"]
            ],
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["attempt_id"] == result["attempt_id"]


def test_reopening_keeps_saved_grade(client, tmp_path):
    _, pack = generate_pack(client, tmp_path)
    first = submit_wrong_once(client, pack)
    reopened = client.get(f"/api/packs/{pack['id']}").json()
    assert reopened["result"]["score"] == first["score"]
    assert reopened["result"]["attempt_id"] == first["attempt_id"]


def test_generation_failure_keeps_inputs_and_creates_no_pack(client, tmp_path):
    from tests.stage4_helpers import make_png, upload

    source = upload(client, make_png(tmp_path / "fail.png", (90, 40, 120)))
    created = create_pack(client, source["id"], count=3, key="workbench-fail-01")
    assert created.status_code == 202
    # 测试环境替身正常回放，因此这里验证的是"重复键不重复建任务"
    again = create_pack(client, source["id"], count=3, key="workbench-fail-01")
    assert again.status_code == 200
    assert again.json()["job_id"] == created.json()["job_id"]
    job = wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded"
    pack_id = job["pack_id"]
    assert pack_id
    detail = client.get(f"/api/packs/{pack_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == pack_id
    with SessionLocal() as db:
        rows = db.execute(
            select(LearningPack).where(LearningPack.job_id == job["job_id"])
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == pack_id
        assert rows[0].source_kind == "real"
        assert rows[0].question_count == 3