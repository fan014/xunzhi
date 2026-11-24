from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "tests" / ".model-settings-real"
SOURCE_DATA = Path("D:/shiyan/xunzhi-phase2-_hc4zu11/data")
PACK_ID = "pack_01M8565B7ZPEY21PD3W62S3CTH"
PORT = 8038
BASE = f"http://127.0.0.1:{PORT}"

shutil.rmtree(DATA_DIR, ignore_errors=True)
if SOURCE_DATA.exists():
    shutil.copytree(SOURCE_DATA, DATA_DIR)
else:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["XUNZHI_DATA_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ROOT_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ALLOW_FAKE"] = "0"
os.environ.pop("DEEPSEEK_API_KEY", None)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
ENV_KEY = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()

import httpx
import uvicorn

from app.main import app

SHOTS = ROOT / "screenshots"


def server():
    uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=PORT,
            log_level="info",
            access_log=False,
        )
    ).run()


def wait_health(client):
    for _ in range(100):
        try:
            response = client.get(f"{BASE}/api/health", timeout=2)
            if response.status_code == 200:
                return response.json()
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("server did not start")


def main():
    report = {
        "pack_id": PACK_ID,
        "env_key_present": bool(ENV_KEY),
        "screenshots": [],
        "checks": {},
    }
    if not ENV_KEY:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=BASE, timeout=300) as client:
            health = wait_health(client)
            report["health"] = {"user_id": health.get("user_id"), "desktop": health.get("desktop_mode")}
            status = client.get("/api/settings/model").json()
            report["initial_model"] = {
                key: status.get(key)
                for key in ("configured", "source", "source_label", "masked", "fake_mode", "model")
            }
            probe = client.post("/api/settings/model/test", json={"api_key": None}).json()
            report["probe_env"] = {
                key: probe.get(key)
                for key in ("ok", "code", "mode", "http_status")
            }

            invalid_key = "sk-invalid-probe-000000000000"
            invalid_probe = client.post(
                "/api/settings/model/test", json={"api_key": invalid_key}
            ).json()
            report["probe_invalid"] = {
                key: invalid_probe.get(key)
                for key in ("ok", "code", "mode", "http_status")
            }

            rejected = client.put(
                "/api/settings/model", json={"api_key": invalid_key}
            )
            report["save_invalid"] = {
                "status": rejected.status_code,
                "error_code": (rejected.json().get("error") or {}).get("code"),
                "original_available": "原配置仍可用" in (
                    (rejected.json().get("error") or {}).get("message") or ""
                ),
            }
            after_reject = client.get("/api/settings/model").json()
            report["after_reject"] = {
                key: after_reject.get(key)
                for key in ("configured", "source", "source_label", "masked")
            }
            retry_original = client.post(
                "/api/settings/model/test", json={"api_key": None}
            ).json()
            report["probe_after_reject"] = {
                key: retry_original.get(key)
                for key in ("ok", "code", "mode", "http_status")
            }

            accepted = client.put(
                "/api/settings/model", json={"api_key": ENV_KEY}
            )
            report["save_valid"] = {
                "status": accepted.status_code,
                "source": (accepted.json().get("source") if accepted.status_code == 200 else None),
            }

            pack = client.get(f"/api/packs/{PACK_ID}").json()
            result = pack.get("result") or {}
            attempt_id = result.get("attempt_id")
            if not attempt_id:
                con = sqlite3.connect(DATA_DIR / "xunzhi.sqlite3")
                row = con.execute(
                    "select id from attempt where pack_id=? and status='submitted' order by submitted_at desc limit 1",
                    (PACK_ID,),
                ).fetchone()
                con.close()
                attempt_id = row[0] if row else None
            report["attempt_id"] = attempt_id
            if attempt_id:
                assessed = client.post(
                    f"/api/packs/{PACK_ID}/assessment",
                    json={"attempt_id": attempt_id, "force": True},
                )
                body = assessed.json()
                report["assessment"] = {
                    "status_code": assessed.status_code,
                    "status": body.get("status"),
                    "multi_source": body.get("multi_source"),
                    "sources": [
                        {
                            "source_id": item.get("source_id"),
                            "status": item.get("status"),
                            "current_evidence_count": item.get("current_evidence_count"),
                        }
                        for item in (body.get("sources") or [])
                    ],
                }
            client.post("/api/settings/model/reset")
            reset = client.get("/api/settings/model").json()
            report["after_reset"] = {
                key: reset.get(key)
                for key in ("configured", "source", "source_label", "masked")
            }

            log_path = ROOT / "tests" / ".model-settings-real.log"
            log_text = ""
            if log_path.exists():
                log_text = log_path.read_text(encoding="utf-8", errors="ignore")
            report["key_in_log"] = bool(ENV_KEY and ENV_KEY in log_text)
            report["key_in_report"] = bool(ENV_KEY and ENV_KEY in json.dumps(report, ensure_ascii=False))
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()