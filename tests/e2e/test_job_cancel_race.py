from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from app.domain import generation
from app.infra.model_client import ModelError, ModelResult
from tests.stage4_helpers import create_pack, make_png, upload, wait_job

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "model_output" / "generate_3q_ok.json"


class BlockingModel:
    def __init__(self, started: threading.Event, release: threading.Event, fail: bool = False):
        self.started = started
        self.release = release
        self.fail = fail

    def generate(self, request):
        self.started.set()
        if not self.release.wait(timeout=10):
            raise RuntimeError("blocking model was not released")
        if self.fail:
            raise ModelError("generation_timeout", "晚到的模型错误")
        return ModelResult(
            content_text=FIXTURE.read_text(encoding="utf-8"),
            finish_reason="stop",
            usage={},
            latency_ms=1,
        )


def count_packs(client) -> int:
    return len(client.get("/api/packs?limit=200").json()["items"])


def cleanup(client, source, pack_id=None):
    if pack_id:
        client.request("DELETE", f"/api/packs/{pack_id}", json={"confirm": True})
    client.request("DELETE", f"/api/sources/{source['id']}", json={"confirm": True})


def wrap_run_job(monkeypatch, finished: threading.Event) -> None:
    original = generation.run_job

    def wrapped(job_id: str):
        try:
            original(job_id)
        finally:
            finished.set()

    monkeypatch.setattr(generation, "run_job", wrapped)


def start_blocked_job(client, tmp_path, monkeypatch, fail: bool = False):
    source = upload(client, make_png(tmp_path / "cancel_race.png", color=(40, 120, 160)))
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    model = BlockingModel(started, release, fail=fail)
    monkeypatch.setattr(generation, "get_model_client", lambda: model)
    wrap_run_job(monkeypatch, finished)
    response = create_pack(client, source["id"], count=3, key=f"cancel-race-{uuid.uuid4().hex}")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert started.wait(timeout=10), "model was not called"
    return source, job_id, release, finished


def test_cancel_wins_over_late_success_and_no_pack_is_created(client, tmp_path, monkeypatch):
    source, job_id, release, finished = start_blocked_job(client, tmp_path, monkeypatch)
    before = count_packs(client)
    try:
        cancelled = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] == "cancelled"

        release.set()
        assert finished.wait(timeout=10), "generation worker did not exit"
        job = wait_job(client, job_id)
        assert job["status"] == "cancelled"
        assert job["pack_id"] is None
        assert count_packs(client) == before
    finally:
        release.set()
        finished.wait(timeout=10)
        cleanup(client, source)


def test_cancel_wins_over_late_model_error(client, tmp_path, monkeypatch):
    source, job_id, release, finished = start_blocked_job(client, tmp_path, monkeypatch, fail=True)
    before = count_packs(client)
    try:
        cancelled = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancelled.status_code == 202
        release.set()
        assert finished.wait(timeout=10), "generation worker did not exit"
        job = wait_job(client, job_id)
        assert job["status"] == "cancelled"
        assert job["pack_id"] is None
        assert count_packs(client) == before
    finally:
        release.set()
        finished.wait(timeout=10)
        cleanup(client, source)


def test_cancel_after_success_returns_409_and_keeps_pack(client, tmp_path, monkeypatch):
    source = upload(client, make_png(tmp_path / "cancel_after_success.png", color=(90, 60, 140)))
    pack_id = None
    release = threading.Event()
    release.set()
    finished = threading.Event()
    model = BlockingModel(threading.Event(), release)
    monkeypatch.setattr(generation, "get_model_client", lambda: model)
    wrap_run_job(monkeypatch, finished)
    try:
        response = create_pack(client, source["id"], count=3, key="cancel-after-success-01")
        assert response.status_code == 202
        assert finished.wait(timeout=10), "generation worker did not exit"
        job = wait_job(client, response.json()["job_id"])
        assert job["status"] == "succeeded"
        pack_id = job["pack_id"]
        assert pack_id

        cancelled = client.post(f"/api/jobs/{job['job_id']}/cancel")
        assert cancelled.status_code == 409
        assert cancelled.json()["error"]["code"] == "job_finished"
        assert client.get(f"/api/packs/{pack_id}").status_code == 200
    finally:
        release.set()
        finished.wait(timeout=10)
        cleanup(client, source, pack_id)