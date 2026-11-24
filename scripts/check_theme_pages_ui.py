from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "tests" / ".theme-ui"
SOURCE_DATA = Path("D:/shiyan/xunzhi-phase2-_hc4zu11/data")
PACK_ID = "pack_01M8565B7ZPEY21PD3W62S3CTH"
PLAN_ID = "plan_01M85643XC6HP9BW5E100TF57T"
PRACTICE_TASK = "sch_01M85643Y14KD293V5REEK7QCH"
PORT = 8037
BASE = f"http://127.0.0.1:{PORT}"

shutil.rmtree(DATA_DIR, ignore_errors=True)
if SOURCE_DATA.exists():
    shutil.copytree(SOURCE_DATA, DATA_DIR)
else:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["XUNZHI_DATA_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ROOT_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ALLOW_FAKE"] = "1"
os.environ["DEEPSEEK_API_KEY"] = ""

import httpx
import uvicorn
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from app.main import app

SHOTS = ROOT / "screenshots"


def server():
    uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    ).run()


def wait_health(client):
    for _ in range(80):
        try:
            if client.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("server did not start")


def main():
    SHOTS.mkdir(exist_ok=True)
    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    report = {"screenshots": [], "checks": {}}
    try:
        with httpx.Client(base_url=BASE, timeout=60) as client:
            wait_health(client)
            client.put(
                "/api/settings/appearance",
                json={"theme": "dark", "font_size": "large", "font_family": "reading"},
            )
        from selenium.webdriver.edge.options import Options as EdgeOptions

        options = EdgeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,900")
        driver = webdriver.Edge(options=options)
        wait = WebDriverWait(driver, 20)
        try:
            driver.get(f"{BASE}/packs/{PACK_ID}")
            wait.until(lambda d: d.find_element(By.ID, "reader-title").text)
            report["checks"]["reader_title"] = driver.find_element(By.ID, "reader-title").text
            report["checks"]["reader_dark"] = (
                driver.execute_script("return getComputedStyle(document.body).backgroundColor")
                == "rgb(17, 22, 29)"
            )
            driver.save_screenshot(str(SHOTS / "phase3_reader_dark.png"))
            report["screenshots"].append("screenshots/phase3_reader_dark.png")

            driver.get(f"{BASE}/packs/{PACK_ID}?task_id={PRACTICE_TASK}&view=questions")
            wait.until(lambda d: d.find_element(By.ID, "view-questions").is_displayed())
            wait.until(lambda d: "已作答 0 / 4" in d.find_element(By.ID, "answered-count").text)
            report["checks"]["practice_dark"] = (
                driver.execute_script("return getComputedStyle(document.body).backgroundColor")
                == "rgb(17, 22, 29)"
            )
            driver.save_screenshot(str(SHOTS / "phase3_practice_dark.png"))
            report["screenshots"].append("screenshots/phase3_practice_dark.png")

            driver.get(f"{BASE}/calendar?plan_id={PLAN_ID}&date=2026-11-26")
            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, ".schedule-task-actions button"))
            reschedule = next(
                (
                    button
                    for button in driver.find_elements(
                        By.CSS_SELECTOR, ".schedule-task-actions button"
                    )
                    if button.text.strip() == "改期"
                ),
                None,
            )
            assert reschedule is not None
            reschedule.click()
            wait.until(lambda d: d.find_element(By.ID, "reschedule-dialog").is_displayed())
            report["checks"]["reschedule_dark"] = (
                driver.execute_script("return getComputedStyle(document.body).backgroundColor")
                == "rgb(17, 22, 29)"
            )
            driver.save_screenshot(str(SHOTS / "phase3_reschedule_dark.png"))
            report["screenshots"].append("screenshots/phase3_reschedule_dark.png")
            driver.find_element(By.ID, "reschedule-cancel").click()

            driver.get(f"{BASE}/calendar?plan_id={PLAN_ID}&date=2026-11-24")
            wait.until(lambda d: d.find_element(By.ID, "brief-panel").is_displayed())
            report["checks"]["brief_text"] = driver.find_element(By.ID, "brief-summary").text
            driver.execute_script(
                "arguments[0].scrollIntoView();", driver.find_element(By.ID, "brief-panel")
            )
            driver.save_screenshot(str(SHOTS / "phase3_brief_dark.png"))
            report["screenshots"].append("screenshots/phase3_brief_dark.png")
        finally:
            driver.quit()
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()