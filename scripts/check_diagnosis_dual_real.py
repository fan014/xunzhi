from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data_diagnosis_dual_real"
LOG = ROOT / "diagnosis_dual_real.log"
OUT = ROOT / "diagnosis_dual_real.json"
PORT = 8024
BASE = f"http://127.0.0.1:{PORT}"


def _wait_ready(server: subprocess.Popen) -> None:
    deadline = time.time() + 40
    while time.time() < deadline:
        if server.poll() is not None:
            raise SystemExit("服务提前退出，请查看 diagnosis_dual_real.log")
        try:
            if httpx.get(f"{BASE}/api/health", timeout=1).json().get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise SystemExit("服务未在 40 秒内就绪")


def _public_text() -> Path:
    path = ROOT / "acceptance_texts" / "diagnosis_dual_public.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        "IPv4 子网划分：/26 的子网掩码是 255.255.255.192。"
        "192.168.1.0/26 覆盖 192.168.1.0–63，192.168.1.64/26 覆盖 192.168.1.64–127，"
        "所以 PC-A 192.168.1.10 与 PC-B 192.168.1.70 不在同一子网。\n\n"
        "TCP 快速重传：接收端连续收到三个重复 ACK 时，发送端立即重传丢失的报文段，"
        "不等待重传计时器到期。\n",
        encoding="utf-8",
    )
    return path


def _parse_log_lines() -> list[dict]:
    rows = []
    if not LOG.exists():
        return rows
    pattern_ok = re.compile(
        r"diagnosis parse ok session=(?P<session>\S+) attempt=(?P<attempt>\d+) "
        r"finish_reason=(?P<finish>\S+) length=(?P<length>\d+) objects=(?P<objects>\d+) "
        r"used=first fence=(?P<fence>\w+) recovered=(?P<recovered>\w+)"
    )
    pattern_fail = re.compile(
        r"diagnosis parse failed session=(?P<session>\S+) attempt=(?P<attempt>\d+) "
        r"finish_reason=(?P<finish>\S+) length=(?P<length>\d+) objects=(?P<objects>\d+) "
        r"position=(?P<position>\S+) reason=(?P<reason>\S+)"
    )
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        ok = pattern_ok.search(line)
        if ok:
            rows.append({"status": "ok", **ok.groupdict()})
            continue
        fail = pattern_fail.search(line)
        if fail:
            rows.append({"status": "failed", **fail.groupdict()})
    return rows


def main() -> int:
    shutil.rmtree(DATA, ignore_errors=True)
    LOG.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "XUNZHI_DATA_DIR": str(DATA),
        "XUNZHI_PORT": str(PORT),
    }
    env.pop("XUNZHI_ALLOW_FAKE", None)
    log_handle = open(LOG, "w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "scripts/run_server.py", "--port", str(PORT), "--no-open"],
        cwd=str(ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    result: dict = {"port": PORT, "is_real_model": True}
    try:
        _wait_ready(server)
        with httpx.Client(timeout=240) as client:
            note = _public_text()
            with open(note, "rb") as fh:
                uploaded = client.post(
                    f"{BASE}/api/sources", files={"file": (note.name, fh, "text/plain")}
                )
            uploaded.raise_for_status()
            source = uploaded.json()["source"]
            result["source_id"] = source["id"]
            started = client.post(
                f"{BASE}/api/sources/{source['id']}/diagnosis/sessions",
                json={
                    "idempotency_key": f"dual-real-{time.time_ns()}",
                    "goal": "IPv4 子网划分与 TCP 快速重传",
                    "question_count": 3,
                },
            )
            result["http_status"] = started.status_code
            payload = started.json()
            result["error"] = payload.get("error")
            session = payload.get("session") or {}
            result["session_id"] = session.get("id")
            question = session.get("current_question")
            result["has_question"] = question is not None
            result["question_stem"] = (question or {}).get("stem", "")[:120]
            result["question_refs"] = [
                item.get("ref") for item in (question or {}).get("citations") or []
            ]
            result["finish_reason_from_log"] = None
            result["log_recovery"] = [row for row in _parse_log_lines() if row.get("session") == session.get("id")]
            if result["log_recovery"]:
                last = result["log_recovery"][-1]
                result["finish_reason_from_log"] = last.get("finish")
                result["first_turn_object_count"] = int(last.get("objects", 0))
                result["recovered_from_multiple_objects"] = last.get("recovered") == "True"
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        log_handle.close()
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())