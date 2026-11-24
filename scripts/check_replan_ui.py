from __future__ import annotations

import json
import os
import shutil
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
DATA = ROOT / "data_ui_replan"
SHOTS = ROOT / "screenshots"
PORT = 8026
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
    driver = None
    result: dict = {"port": PORT}
    try:
        _wait_ready()
        os.environ.update(env)
        sys.path.insert(0, str(ROOT))
        seed_dir = ROOT / "acceptance_texts" / "ui_replan"
        seed_dir.mkdir(parents=True, exist_ok=True)
        from tests.contract.test_diagnosis_replan import _seed_confirmed_session

        with httpx.Client(base_url=BASE, timeout=60) as client:
            seeded = _seed_confirmed_session(client, seed_dir)
            result.update(
                {
                    "session_id": seeded["session_id"],
                    "old_plan_id": seeded["old_plan_id"],
                    "old_version_id": seeded["old_version_id"],
                    "old_job_id": seeded["old_job_id"],
                    "evidence_before": seeded["evidence_before"],
                }
            )
        driver = _browser()
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
        )
        wait = WebDriverWait(driver, 30)
        driver.get(f"{BASE}/diagnosis/{seeded['session_id']}")
        wait.until(lambda d: d.find_element(By.ID, "replan-plan").is_displayed())
        driver.refresh()
        wait.until(lambda d: d.find_element(By.ID, "replan-plan").is_displayed())
        result["failure_status"] = driver.find_element(By.ID, "generation-status").text
        result["retry_button_before"] = bool(driver.find_elements(By.ID, "retry-generation"))
        assert result["retry_button_before"] is False
        result["viewport"] = driver.execute_script("return [window.innerWidth, window.innerHeight]")
        driver.save_screenshot(str(SHOTS / "replan_390_before.png"))

        driver.find_element(By.ID, "replan-plan").click()
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#plan-preview button.primary"))
        result["plan_visible"] = driver.find_element(By.ID, "plan-preview").is_displayed()
        result["plan_text"] = driver.find_element(By.ID, "plan-preview").text[:500]
        driver.save_screenshot(str(SHOTS / "replan_390_plan.png"))

        driver.find_element(By.CSS_SELECTOR, "#plan-preview button.primary").click()
        wait.until(lambda d: d.find_elements(By.ID, "open-pack"))
        result["pack_href"] = driver.find_element(By.ID, "open-pack").get_attribute("href")
        result["final_status"] = driver.find_element(By.ID, "generation-status").text
        driver.save_screenshot(str(SHOTS / "replan_390_pack.png"))

        with sqlite3.connect(DATA / "xunzhi.sqlite3") as con:
            evidence_after = con.execute(
                "select count(*) from learning_evidence"
            ).fetchone()[0]
            old_job = con.execute(
                "select status, error_code from generation_job where id = ?",
                (seeded["old_job_id"],),
            ).fetchone()
            old_plan = con.execute(
                "select count(*) from learning_plan where id = ?",
                (seeded["old_plan_id"],),
            ).fetchone()[0]
            old_version = con.execute(
                "select count(*) from learning_plan_version where id = ?",
                (seeded["old_version_id"],),
            ).fetchone()[0]
            container = con.execute(
                "select count(*) from learning_pack where job_id = ?",
                (f"diagnostic:{seeded['session_id']}",),
            ).fetchone()[0]
        result.update(
            {
                "evidence_after": evidence_after,
                "old_job": list(old_job) if old_job else None,
                "old_plan_exists": bool(old_plan),
                "old_version_exists": bool(old_version),
                "diagnostic_container_count": int(container),
            }
        )
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