from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data_ui_cancel"
SHOTS = ROOT / "screenshots"
PORT = 8021
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
    note = ROOT / "acceptance_texts" / "ui_cancel_note.txt"
    note.parent.mkdir(exist_ok=True)
    note.write_text(
        "第一段：交换机按 MAC 地址表转发帧。\n\n第二段：路由器按路由表转发跨网段分组。",
        encoding="utf-8",
    )
    with open(note, "rb") as fh:
        response = client.post(
            f"{BASE}/api/sources",
            files={"file": (note.name, fh, "text/plain")},
        )
    response.raise_for_status()
    source = response.json()["source"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    suffix = str(time.time_ns())
    plan_id = f"plan_ui_cancel_{suffix}"
    version_id = f"pv_ui_cancel_{suffix}"
    session_id = f"dgs_ui_cancel_{suffix}"
    job_id = f"job_ui_cancel_{suffix}"
    con = sqlite3.connect(DATA / "xunzhi.sqlite3")
    try:
        con.execute(
            "insert into learning_plan (id, title, status, created_at, updated_at) values (?,?,?,?,?)",
            (plan_id, "取消生成 UI 验收", "confirmed", now, now),
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
            "plan_version_id, created_at, updated_at, confirmed_at) "
            "values (?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                source["id"],
                "取消生成 UI 验收",
                "confirmed",
                0,
                f"ui-cancel-{suffix}",
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
                "write_handout",
                now,
                "{}",
                "ui-cancel",
                now,
                now,
                "dispatched",
            ),
        )
        con.commit()
    finally:
        con.close()
    return {"source": source, "plan_id": plan_id, "version_id": version_id, "session_id": session_id, "job_id": job_id}


def _browser():
    from selenium.webdriver.edge.options import Options as EdgeOptions

    options = EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=390,844")
    return webdriver.Edge(options=options)


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    shutil.rmtree(DATA, ignore_errors=True)
    env = {
        **os_environ(),
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
            driver.set_window_size(390, 844)
            driver.execute_cdp_cmd(
                "Emulation.setDeviceMetricsOverride",
                {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
            )
            wait = WebDriverWait(driver, 20)
            driver.get(f"{BASE}/diagnosis/{seeded['session_id']}")
            wait.until(lambda d: d.find_element(By.ID, "cancel-generation").is_displayed())
            driver.save_screenshot(str(SHOTS / "cancel_390_before.png"))
            driver.execute_script(
                "window.__cancelCalls = 0;"
                "const original = window.fetch;"
                "window.fetch = function(url, opts) {"
                "  if (String(url).indexOf('/cancel') >= 0) window.__cancelCalls += 1;"
                "  return original.apply(this, arguments);"
                "};"
            )
            driver.execute_script(
                "const button = document.getElementById('cancel-generation');"
                "button.focus();"
            )
            result["focused_control"] = driver.execute_script("return document.activeElement.id")
            cancel_button = driver.find_element(By.ID, "cancel-generation")
            cancel_button.send_keys(Keys.ENTER)
            driver.execute_script(
                "const button = document.getElementById('cancel-generation');"
                "if (button) { button.click(); button.click(); }"
            )
            wait.until(
                lambda d: "已取消，资料和学习计划已保留" in d.find_element(By.ID, "generation-status").text
            )
            result["status_text"] = driver.find_element(By.ID, "generation-status").text
            result["cancel_calls"] = driver.execute_script("return window.__cancelCalls")
            result["cancel_button_after"] = driver.find_elements(By.ID, "cancel-generation") != []
            result["retry_button_after"] = driver.find_elements(By.ID, "retry-generation") != []
            result["viewport"] = driver.execute_script("return [window.innerWidth, window.innerHeight]")
            driver.save_screenshot(str(SHOTS / "cancel_390_after.png"))

            job = client.get(f"{BASE}/api/jobs/{seeded['job_id']}").json()
            result["api_status"] = job["status"]
            result["api_pack_id"] = job["pack_id"]

            driver.refresh()
            wait.until(
                lambda d: "已取消" in d.find_element(By.ID, "generation-status").text
            )
            result["refreshed_status_text"] = driver.find_element(By.ID, "generation-status").text
            result["refreshed_cancel_button"] = driver.find_elements(By.ID, "cancel-generation") != []
            result["refreshed_retry_button"] = driver.find_elements(By.ID, "retry-generation") != []
            result["refreshed_has_open_pack"] = driver.find_elements(By.ID, "open-pack") != []
            driver.save_screenshot(str(SHOTS / "cancel_390_refreshed.png"))
    finally:
        if driver is not None:
            driver.quit()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    con = sqlite3.connect(DATA / "xunzhi.sqlite3")
    try:
        packs = con.execute(
            "select count(*) from learning_pack where job_id = ?", (result["job_id"],)
        ).fetchone()[0]
    finally:
        con.close()
    result["packs_for_job"] = packs
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def os_environ() -> dict:
    import os

    return dict(os.environ)


if __name__ == "__main__":
    raise SystemExit(main())