from __future__ import annotations

import threading
from dataclasses import replace

from sqlalchemy import select

from app.config import settings
from app.domain import generation
from app.domain.generation import create_job
from app.domain.models import LearningPack
from app.infra.db import SessionLocal
from app.infra.model_client import FakeModelClient
from tests.contract.test_generation_review_retry import _constraints, _payload, _source


class SlowFake(FakeModelClient):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        return super().generate(request)


def test_cancel_queued_job_returns_202_then_409(client, tmp_path):
    source = _source(client, tmp_path, "cancel_queued.txt")
    payload = _payload(source["id"], _constraints(source["id"]), f"pv_cancel_{id(source)}")
    with SessionLocal() as db:
        job, _ = create_job(db, payload)
        db.commit()
        job_id = job.id

    first = client.post(f"/api/jobs/{job_id}/cancel")
    assert first.status_code == 202, first.text
    assert first.json() == {"status": "cancelled"}

    second = client.post(f"/api/jobs/{job_id}/cancel")
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "job_finished"

    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["status"] == "cancelled"
    assert status["pack_id"] is None


def test_cancel_running_job_stops_dispatch_and_publishes_nothing(client, tmp_path, monkeypatch):
    source = _source(client, tmp_path, "cancel_running.txt")
    payload = _payload(source["id"], _constraints(source["id"]), f"pv_cancel_run_{id(source)}")
    slow = SlowFake()
    monkeypatch.setattr(generation, "get_model_client", lambda: slow)
    monkeypatch.setattr(generation, "settings", replace(settings, generation_max_parallel=1))

    with SessionLocal() as db:
        job, _ = create_job(db, payload)
        db.commit()
        job_id = job.id

    worker = threading.Thread(target=generation.run_job, args=(job_id,), daemon=True)
    worker.start()
    assert slow.started.wait(5), "生成任务未开始调用模型"

    cancelled = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancelled.status_code == 202, cancelled.text
    slow.release.set()
    worker.join(10)
    assert not worker.is_alive(), "取消后执行器未停止"

    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["status"] == "cancelled"
    assert status["pack_id"] is None
    assert slow.calls == 1
    with SessionLocal() as db:
        packs = db.execute(
            select(LearningPack).where(LearningPack.job_id == job_id)
        ).scalars().all()
        assert packs == []