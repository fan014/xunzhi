from __future__ import annotations

import time
from pathlib import Path

from PIL import Image, ImageDraw

PROGRESS = "我学完了交换机转发，还没学路由协议。同一网段和跨网段发包容易混，想用这张图练一下。"


def make_png(path: Path, color: tuple[int, int, int] = (20, 120, 80)) -> Path:
    img = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([30, 60, 170, 130], outline=color, width=3)
    draw.text((55, 85), "PC-A", fill="#17211c")
    draw.rectangle([30, 340, 170, 410], outline=color, width=3)
    draw.text((55, 365), "PC-B", fill="#17211c")
    draw.rectangle([600, 60, 760, 130], outline=color, width=3)
    draw.text((640, 85), "PC-C", fill="#17211c")
    draw.rectangle([330, 220, 470, 290], outline=(40, 100, 150), width=3)
    draw.text((370, 245), "SW1", fill="#17211c")
    draw.rectangle([620, 320, 780, 390], outline=(150, 90, 40), width=3)
    draw.text((670, 345), "R1", fill="#17211c")
    draw.line([170, 95, 330, 240], fill="#5b6b62", width=3)
    draw.line([170, 375, 330, 270], fill="#5b6b62", width=3)
    draw.line([470, 255, 690, 95], fill="#5b6b62", width=3)
    draw.line([650, 280, 700, 320], fill="#5b6b62", width=3)
    draw.text((210, 150), "192.168.1.0/24", fill="#5b6b62")
    draw.text((510, 165), "192.168.2.0/24", fill="#5b6b62")
    img.save(path, format="PNG")
    return path


def upload(client, path: Path, name: str | None = None) -> dict:
    with open(path, "rb") as fh:
        response = client.post(
            "/api/sources",
            files={"file": (name or path.name, fh, "application/octet-stream")},
        )
    assert response.status_code == 201, response.text
    return response.json()["source"]


def create_pack(
    client,
    source_id: str | None,
    count: int = 3,
    key: str | None = None,
    parent_pack_id: str | None = None,
    review_focus: list[str] | None = None,
    progress: str = PROGRESS,
):
    body = {
        "idempotency_key": key or f"stage4-{time.time_ns()}",
        "sources": [{"source_id": source_id, "page_start": None, "page_end": None}] if source_id else [],
        "progress_text": progress,
        "difficulty": "consolidate",
        "question_count": count,
        "consent_ack": True,
        "parent_pack_id": parent_pack_id,
        "review_focus": review_focus or [],
    }
    return client.post("/api/packs", json=body)


def wait_job(client, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/jobs/{job_id}").json()
        if data["status"] in {"succeeded", "failed", "insufficient_evidence", "cancelled"}:
            return data
        time.sleep(0.2)
    raise AssertionError("job did not finish in time")


def generate_pack(client, tmp_path: Path, color=(20, 120, 80), count: int = 3) -> tuple[dict, dict]:
    source = upload(client, make_png(tmp_path / f"topology_{time.time_ns()}.png", color))
    created = create_pack(client, source["id"], count=count)
    assert created.status_code == 202, created.text
    job = wait_job(client, created.json()["job_id"])
    assert job["status"] == "succeeded", job
    detail = client.get(f"/api/packs/{job['pack_id']}").json()
    return source, detail


def submit_wrong_once(client, pack: dict) -> dict:
    answers = []
    for question in pack["questions"]:
        answers.append({"question_id": question["id"], "selected_index": 0})
    return client.post(
        f"/api/packs/{pack['id']}/submit",
        json={"idempotency_key": f"submit-{time.time_ns()}", "answers": answers},
    ).json()