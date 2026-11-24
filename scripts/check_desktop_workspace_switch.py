from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "tests" / ".desktop-switch"
SOURCE_DATA = Path("D:/shiyan/xunzhi-phase2-_hc4zu11/data")
SOURCE_PACK = "pack_01M8565B7ZPEY21PD3W62S3CTH"
shutil.rmtree(DATA_DIR, ignore_errors=True)
if SOURCE_DATA.exists():
    shutil.copytree(SOURCE_DATA, DATA_DIR)
else:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
os.environ["XUNZHI_DATA_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ROOT_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_PORT"] = str(PORT)
os.environ["XUNZHI_DESKTOP"] = "1"
os.environ["XUNZHI_DESKTOP_DIAGNOSE"] = "1"
os.environ["XUNZHI_ALLOW_FAKE"] = "1"
os.environ["DEEPSEEK_API_KEY"] = ""

import httpx

from scripts import desktop_launcher


def wait_health(timeout=60.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with httpx.Client(base_url=BASE, timeout=3) as client:
                response = client.get("/api/health")
                if response.status_code == 200:
                    return response.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.4)
    raise RuntimeError(f"health not ready: {last}")


def main():
    stop = threading.Event()
    desktop_launcher._open_window = lambda url, logger: stop.wait(180)
    result = {}

    def run():
        result["code"] = desktop_launcher.main()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    report = {"root": str(DATA_DIR), "port": PORT, "switch": {}}
    try:
        health = wait_health()
        report["initial"] = health
        assert health["user_id"] == "user_default"
        with httpx.Client(base_url=BASE, timeout=30) as client:
            initial_schedule = client.get("/api/schedule").json()
            report["a_initial_plans"] = len(initial_schedule.get("plans") or [])
            if SOURCE_DATA.exists():
                assert initial_schedule.get("plans"), "default workspace should keep existing plans"
                assert client.get(f"/api/packs/{SOURCE_PACK}").status_code == 200
            a_theme = client.put(
                "/api/settings/appearance",
                json={"theme": "dark", "font_size": "large", "font_family": "reading"},
            ).json()
            report["a_theme"] = a_theme
            assert a_theme["theme"] == "dark"
            pack_detail = client.get(f"/api/packs/{SOURCE_PACK}").json()
            question_id = pack_detail["questions"][0]["id"]
            client.post(f"/api/packs/{SOURCE_PACK}/reattempt")
            draft = client.put(
                f"/api/packs/{SOURCE_PACK}/draft",
                json={"answers": [{"question_id": question_id, "selected_index": 1}]},
            )
            report["a_draft_write_status"] = draft.status_code
            assert draft.status_code == 200, draft.text
            report["a_draft_question"] = question_id

            created = client.post(
                "/api/settings/users", json={"display_name": "桌面学习者 B"}
            ).json()["user"]
            report["created"] = created
            switched = client.post("/api/settings/switch", json={"user_id": created["id"]})
            report["switch"]["status_code"] = switched.status_code
            report["switch"]["body"] = switched.json()
            assert switched.status_code == 202, switched.text

        deadline = time.time() + 60
        switched_health = None
        while time.time() < deadline:
            try:
                with httpx.Client(base_url=BASE, timeout=3) as client:
                    candidate = client.get("/api/health").json()
                    if candidate.get("user_id") == created["id"]:
                        switched_health = candidate
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        assert switched_health is not None, "did not switch to B"
        report["after_switch"] = switched_health
        with httpx.Client(base_url=BASE, timeout=30) as client:
            schedule = client.get("/api/schedule").json()
            report["b_schedule"] = {"plans": len(schedule.get("plans") or []), "plan": schedule.get("plan")}
            assert not schedule.get("plans")
            if SOURCE_DATA.exists():
                blocked = client.get(f"/api/packs/{SOURCE_PACK}")
                report["b_old_pack_status"] = blocked.status_code
                assert blocked.status_code == 404
            b_theme = client.get("/api/settings/appearance").json()
            report["b_theme_initial"] = b_theme
            assert b_theme["theme"] == "system"
            assert b_theme["font_size"] == "standard"
            client.put(
                "/api/settings/appearance",
                json={"theme": "light", "font_size": "small", "font_family": "system"},
            )
            back = client.post("/api/settings/switch", json={"user_id": "user_default"})
            report["switch_back_status"] = back.status_code
            assert back.status_code == 202, back.text

        deadline = time.time() + 60
        back_health = None
        while time.time() < deadline:
            try:
                with httpx.Client(base_url=BASE, timeout=3) as client:
                    candidate = client.get("/api/health").json()
                    if candidate.get("user_id") == "user_default":
                        back_health = candidate
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
        assert back_health is not None, "did not switch back to default"
        report["after_switch_back"] = back_health
        with httpx.Client(base_url=BASE, timeout=30) as client:
            restored_schedule = client.get("/api/schedule").json()
            report["a_restored_plans"] = len(restored_schedule.get("plans") or [])
            if SOURCE_DATA.exists():
                assert restored_schedule.get("plans")
                restored_pack = client.get(f"/api/packs/{SOURCE_PACK}")
                assert restored_pack.status_code == 200
                restored_draft = restored_pack.json().get("draft", {}).get("answers") or []
                report["a_draft_restored"] = restored_draft
                assert any(item["question_id"] == question_id for item in restored_draft)
            restored_theme = client.get("/api/settings/appearance").json()
            report["a_theme_restored"] = restored_theme
            assert restored_theme["theme"] == "dark"
            assert restored_theme["font_size"] == "large"
        report["result_file"] = json.loads((DATA_DIR / "switch-result.json").read_text(encoding="utf-8"))
        stop.set()
        thread.join(timeout=30)
        report["launcher_exit"] = result.get("code")
        assert result.get("code") == 0
    finally:
        stop.set()
        if thread.is_alive():
            thread.join(timeout=5)
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()