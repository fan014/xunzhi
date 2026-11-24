from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data_ui_cancel_finished"
SHOTS = ROOT / "screenshots"
PORT = 8022
BASE = f"http://127.0.0.1:{PORT}"


def _wait_ready() -> None:
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            if httpx.get(f"{BASE}/api/health", timeout=1).json().get("status") == "ok":
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise SystemExit("服务未在 40 秒内就绪")


def _seed(client: httpx.Client) -> dict:
    note = ROOT / "acceptance_texts" / "ui_cancel_finished.txt"
    note.parent.mkdir(exist_ok=True)
    note.write_text("第一段：交换机按 MAC 地址表转发帧。\n\n第二段：路由器按路由表转发。", encoding="utf-8")
    with open(note, "rb") as fh:
        response = client.post(f"{BASE}/api/sources", files={"file": (note.name, fh, "text/plain")})
    response.raise_for_status()
    source = response.json()["source"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    suffix = str(time.time_ns())
    plan_id = f"plan_ui_finished_{suffix}"
    version_id = f"pv_ui_finished_{suffix}"
    session_id = f"dgs_ui_finished_{suffix}"
    job_id = f"job_ui_finished_{suffix}"
    pack_id = f"pack_ui_finished_{suffix}"
    con = sqlite3.connect(DATA / "xunzhi.sqlite3")
    try:
        con.execute(
            "insert into learning_plan (id, title, status, created_at, updated_at) values (?,?,?,?,?)",
            (plan_id, "完成竞态 UI 验收", "confirmed", now, now),
        )
        con.execute(
            "insert into learning_plan_version "
            "(id, plan_id, status, origin, adjustment_json, sources_json, scope_json, items_json, "
            "question_count, created_at, confirmed_at) values (?,?,?,?,?,?,?,?,?,?,?)",
            (
                version_id,
                plan_id,
                "confirmed",
                "manual",
                "[]",
                json.dumps([{"source_id": source["id"]}], ensure_ascii=False),
                "{}",
                "[]",
                3,
                now,
                now,
            ),
        )
        con.execute(
            "insert into diagnostic_session "
            "(id, source_id, target_goal, status, current_round, session_key, pending_items_json, "
            "plan_version_id, created_at, updated_at, confirmed_at) values (?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                source["id"],
                "完成竞态 UI 验收",
                "confirmed",
                0,
                f"ui-finished-{suffix}",
                json.dumps(
                    {
                        "learned_point_ids": [],
                        "unlearned_point_ids": [],
                        "uncertain": [],
                        "question_count": 3,
                        "learning_mode": "auto",
                    },
                    ensure_ascii=False,
                ),
                version_id,
                now,
                now,
                now,
            ),
        )
        con.execute(
            "insert into generation_job "
            "(id, idempotency_key, status, stage, stage_updated_at, request_json, input_hash, "
            "created_at, updated_at, launch_state) values (?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                f"diagnostic:{session_id}",
                "running",
                "persist",
                now,
                "{}",
                "ui-finished",
                now,
                now,
                "dispatched",
            ),
        )
        con.commit()
    finally:
        con.close()
    return {
        "source": source,
        "plan_id": plan_id,
        "version_id": version_id,
        "session_id": session_id,
        "job_id": job_id,
        "pack_id": pack_id,
    }


def _browser():
    from selenium.webdriver.edge.options import Options as EdgeOptions

    options = EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=390,844")
    return webdriver.Edge(options=options)


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    import shutil

    shutil.rmtree(DATA, ignore_errors=True)
    import os

    env = {
        **os.environ,
        "XUNZHI_DATA_DIR": str(DATA),
        "XUNZHI_ALLOW_FAKE": "1",
        "XUNZHI_FAKE_MODE": "ok",
        "DEEPSEEK_API_KEY": "",
        "XUNZHI_PORT": str(PORT),
    }
    server = subprocess.Popen(
        [sys.executable, "scripts/run_server.py", "--port", str(PORT), "--no-open"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    result: dict = {"port": PORT}
    driver = None
    try:
        _wait_ready()
        with httpx.Client(timeout=30) as client:
            seeded = _seed(client)
            result.update(seeded)
            driver = _browser()
            driver.execute_cdp_cmd(
                "Emulation.setDeviceMetricsOverride",
                {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
            )
            wait = WebDriverWait(driver, 20)
            driver.get(f"{BASE}/diagnosis/{seeded['session_id']}")
            wait.until(lambda d: d.find_element(By.ID, "cancel-generation").is_displayed())

            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            con = sqlite3.connect(DATA / "xunzhi.sqlite3")
            try:
                con.execute(
                    "insert into learning_pack "
                    "(id, display_title, source_kind, difficulty, question_count, job_id, "
                    "handout_word_count, model_name, prompt_version, created_at) "
                    "values (?,?,?,?,?,?,?,?,?,?)",
                    (
                        seeded["pack_id"],
                        "完成竞态学习包",
                        "real",
                        "consolidate",
                        3,
                        seeded["job_id"],
                        0,
                        "fake",
                        "prompt_v1",
                        now,
                    ),
                )
                con.execute(
                    "update generation_job set status='succeeded', pack_id=?, updated_at=? where id=?",
                    (seeded["pack_id"], now, seeded["job_id"]),
                )
                con.commit()
            finally:
                con.close()

            driver.execute_script("document.getElementById('cancel-generation').click();")
            wait.until(lambda d: d.find_elements(By.ID, "open-pack"))
            result["status_text"] = driver.find_element(By.ID, "generation-status").text
            result["open_pack_href"] = driver.find_element(By.ID, "open-pack").get_attribute("href")
            result["cancel_button_after"] = driver.find_elements(By.ID, "cancel-generation") != []
            result["viewport"] = driver.execute_script("return [window.innerWidth, window.innerHeight]")
            driver.save_screenshot(str(SHOTS / "cancel_390_finished.png"))

            job = client.get(f"{BASE}/api/jobs/{seeded['job_id']}").json()
            result["api_status"] = job["status"]
            result["api_pack_id"] = job["pack_id"]
    finally:
        if driver is not None:
            driver.quit()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())