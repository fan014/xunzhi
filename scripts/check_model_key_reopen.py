from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "tests" / ".model-reopen"
LOG = ROOT / "tests" / ".model-reopen.log"
shutil.rmtree(DATA_DIR, ignore_errors=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)


def free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
os.environ["XUNZHI_DATA_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ROOT_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ALLOW_FAKE"] = "0"
os.environ.pop("DEEPSEEK_API_KEY", None)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
KEY = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()

import httpx


def start():
    env = os.environ.copy()
    env.update(
        {
            "XUNZHI_DATA_DIR": str(DATA_DIR),
            "XUNZHI_ROOT_DIR": str(DATA_DIR),
            "XUNZHI_ALLOW_FAKE": "0",
            "XUNZHI_PORT": str(PORT),
            "XUNZHI_HOST": "127.0.0.1",
            "XUNZHI_INSTANCE_ID": f"reopen-{time.time_ns()}",
        }
    )
    handle = open(LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    for _ in range(100):
        try:
            with httpx.Client(base_url=BASE, timeout=3) as client:
                if client.get("/api/health").status_code == 200:
                    return proc, handle
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("server not ready")


def stop(proc, handle):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
    handle.close()


def main():
    report = {"env_key_present": bool(KEY)}
    if not KEY:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    proc, handle = start()
    try:
        with httpx.Client(base_url=BASE, timeout=300) as client:
            saved = client.put("/api/settings/model", json={"api_key": KEY})
            report["save_status"] = saved.status_code
            report["saved_source"] = saved.json().get("source")
            report["saved_masked"] = saved.json().get("masked")
    finally:
        stop(proc, handle)
    proc, handle = start()
    try:
        with httpx.Client(base_url=BASE, timeout=60) as client:
            reopened = client.get("/api/settings/model").json()
            report["reopened"] = {
                key: reopened.get(key)
                for key in ("configured", "source", "source_label", "masked", "fake_mode")
            }
            assert reopened["source"] == "user"
            assert reopened["configured"] is True
            assert reopened["masked"] == report["saved_masked"]
            client.post("/api/settings/model/reset")
            reset = client.get("/api/settings/model").json()
            report["after_reset"] = {
                key: reset.get(key) for key in ("configured", "source", "source_label", "masked")
            }
            assert reset["source"] == "env"
    finally:
        stop(proc, handle)
    log_text = LOG.read_text(encoding="utf-8", errors="ignore") if LOG.exists() else ""
    report["key_in_log"] = bool(KEY and KEY in log_text)
    report["key_in_report"] = bool(KEY and KEY in json.dumps(report, ensure_ascii=False))
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
