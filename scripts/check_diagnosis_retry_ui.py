from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data_ui_diagnosis_retry"
SHOTS = ROOT / "screenshots"
PORT = 8023
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
        [sys.executable, "scripts/ui_stub_server.py"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    driver = None
    result: dict = {"port": PORT}
    try:
        _wait_ready()
        with httpx.Client(timeout=60) as client:
            note = ROOT / "acceptance_texts" / "ui_diagnosis_retry.txt"
            note.parent.mkdir(exist_ok=True)
            note.write_text("交换机根据 MAC 地址表转发帧。路由器根据路由表转发跨网段分组。", encoding="utf-8")
            with open(note, "rb") as fh:
                uploaded = client.post(
                    f"{BASE}/api/sources", files={"file": (note.name, fh, "text/plain")}
                )
            uploaded.raise_for_status()
            source = uploaded.json()["source"]
            started = client.post(
                f"{BASE}/api/sources/{source['id']}/diagnosis/sessions",
                json={"idempotency_key": f"ui-retry-{time.time_ns()}", "goal": "补薄弱点", "question_count": 3},
            )
            started.raise_for_status()
            payload = started.json()
            result["first_error_code"] = (payload.get("error") or {}).get("code")
            session = payload["session"]
            session_id = session["id"]
            result["session_id"] = session_id
            result["first_question"] = session["current_question"]
            result["kept_goal"] = session["goal"]
            result["kept_source_id"] = session["source_id"]

            driver = _browser()
            driver.execute_cdp_cmd(
                "Emulation.setDeviceMetricsOverride",
                {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
            )
            wait = WebDriverWait(driver, 20)
            driver.get(f"{BASE}/diagnosis/{session_id}")
            wait.until(lambda d: "本轮问题生成失败，可重试" in d.find_element(By.ID, "current-question").text)
            result["failure_title"] = driver.find_element(By.CSS_SELECTOR, "#current-question h2").text
            result["failure_hint"] = driver.find_element(By.CSS_SELECTOR, "#current-question p.hint").text
            result["retry_button_before"] = driver.find_elements(By.ID, "retry-turn") != []
            result["viewport"] = driver.execute_script("return [window.innerWidth, window.innerHeight]")
            driver.save_screenshot(str(SHOTS / "diagnosis_390_retry_before.png"))

            driver.find_element(By.ID, "retry-turn").click()
            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, 'input[name="diagnosis-answer"]'))
            result["retry_status"] = driver.find_element(By.ID, "diagnosis-status").text
            result["recovered_title"] = driver.find_element(By.CSS_SELECTOR, "#current-question h2").text
            result["recovered_options"] = len(driver.find_elements(By.CSS_SELECTOR, 'input[name="diagnosis-answer"]'))
            result["submit_visible"] = driver.find_element(By.ID, "submit-answer").is_displayed()
            driver.save_screenshot(str(SHOTS / "diagnosis_390_retry_after.png"))
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