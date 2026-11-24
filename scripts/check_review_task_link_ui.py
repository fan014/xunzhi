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
SOURCE_DATA = Path("D:/shiyan/xunzhi-phase2-_hc4zu11/data")
DATA_DIR = ROOT / "tests" / ".review-link-data"
PORT = 8032
BASE = f"http://127.0.0.1:{PORT}"
PACK_ID = "pack_01M8565B7ZPEY21PD3W62S3CTH"
PLAN_ID = "plan_01M85643XC6HP9BW5E100TF57T"
MODULE_ID = "mod_ad219306d63d"
REVIEW_TASK = "sch_01M85643Y14KD293V5REEK7QCF"
PRACTICE_TASK = "sch_01M85643Y14KD293V5REEK7QCH"

os.environ["XUNZHI_DATA_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ALLOW_FAKE"] = "1"
os.environ["DEEPSEEK_API_KEY"] = ""

import httpx
import uvicorn
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from app.domain import schedule as schedule_service
from app.main import app

SHOTS = ROOT / "screenshots"


def server():
    uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    ).run()


def wait_health(client: httpx.Client):
    for _ in range(80):
        try:
            if client.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("server did not start")


def prepare_data(today: str):
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    shutil.copytree(SOURCE_DATA, DATA_DIR)
    con = sqlite3.connect(DATA_DIR / "xunzhi.sqlite3")
    con.execute(
        "update schedule_task set status='pending', completed_at=NULL, attempt_id=NULL where id=?",
        (REVIEW_TASK,),
    )
    con.commit()
    rows = list(
        con.execute(
            "select id, task_type, scheduled_date, status from schedule_task where plan_version_id='pv_01M85643XC6HP9BW5E100TF57V' order by ordinal"
        )
    )
    con.close()
    return {"today": today, "tasks": [list(row) for row in rows]}


def assert_review(driver, wait):
    wait.until(lambda d: d.find_element(By.ID, "reader-title").text == "模块回顾")
    page_id = driver.execute_script("return window.XunzhiReader.currentPageId()")
    assert page_id == f"recap-{MODULE_ID}", page_id
    assert driver.find_element(By.ID, "view-modules").is_displayed()
    buttons = driver.find_elements(By.TAG_NAME, "button")
    assert any(button.text.strip() == "练本模块" for button in buttons)
    return page_id


def main():
    SHOTS.mkdir(exist_ok=True)
    today = schedule_service.today_local().isoformat()
    report = {"pack_id": PACK_ID, "today": today, "checks": {}, "screenshots": []}
    report["data"] = prepare_data(today)
    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    with httpx.Client(base_url=BASE, timeout=60) as client:
        wait_health(client)
        calendar = client.get(f"/api/schedule?plan_id={PLAN_ID}&date={today}").json()
        report["calendar"] = {
            "today_all_done": calendar["today_all_done"],
            "next_task": calendar["next_task"],
            "brief_next_task": calendar["brief"]["next_task"],
        }
        assert calendar["today_all_done"] is True
        assert calendar["next_task"]["task_type"] == "review"
        expected_link = f"page_id=recap-{MODULE_ID}"
        assert expected_link in calendar["next_task"]["action_url"]
        assert calendar["brief"]["next_task"]["action_url"] == calendar["next_task"]["action_url"]

        from selenium.webdriver.edge.options import Options as EdgeOptions

        options = EdgeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,900")
        driver = webdriver.Edge(options=options)
        wait = WebDriverWait(driver, 20)
        try:
            driver.get(f"{BASE}/packs/{PACK_ID}")
            wait.until(lambda d: d.find_element(By.ID, "reader-title").text)
            resume_page = driver.execute_script(
                "return window.XunzhiReader.currentPageId()"
            )
            report["checks"]["home_resume_page"] = resume_page
            assert resume_page == "opportunity-cost-mechanism", resume_page
            driver.save_screenshot(str(SHOTS / "phase2_review_home_resume.png"))
            report["screenshots"].append("screenshots/phase2_review_home_resume.png")

            driver.get(f"{BASE}/calendar?plan_id={PLAN_ID}&date={today}")
            wait.until(lambda d: d.find_element(By.ID, "selected-actions"))
            next_link = next(
                link
                for link in driver.find_elements(By.CSS_SELECTOR, "#selected-actions a")
                if link.text.strip() == "继续下一项"
            )
            report["checks"]["next_link"] = next_link.get_attribute("href")
            assert expected_link in next_link.get_attribute("href")
            driver.save_screenshot(str(SHOTS / "phase2_review_next_link.png"))
            report["screenshots"].append("screenshots/phase2_review_next_link.png")
            next_link.click()
            report["checks"]["next_page"] = assert_review(driver, wait)
            driver.save_screenshot(str(SHOTS / "phase2_review_next_page.png"))
            report["screenshots"].append("screenshots/phase2_review_next_page.png")

            driver.get(f"{BASE}/calendar?plan_id={PLAN_ID}&date=2026-11-25")
            wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, ".schedule-task-actions a"))
            review_link = next(
                link
                for link in driver.find_elements(
                    By.CSS_SELECTOR, ".schedule-task-actions a"
                )
                if link.text.strip() == "去回顾"
            )
            report["checks"]["calendar_review_link"] = review_link.get_attribute("href")
            assert expected_link in review_link.get_attribute("href")
            review_link.click()
            report["checks"]["calendar_page"] = assert_review(driver, wait)
            driver.save_screenshot(str(SHOTS / "phase2_review_calendar_page.png"))
            report["screenshots"].append("screenshots/phase2_review_calendar_page.png")

            driver.get(f"{BASE}/calendar?plan_id={PLAN_ID}&date={today}")
            wait.until(lambda d: d.find_element(By.ID, "brief-panel").is_displayed())
            brief_link = next(
                link
                for link in driver.find_elements(By.CSS_SELECTOR, "#brief-actions a")
                if link.text.strip() == "继续下一项"
            )
            report["checks"]["brief_link"] = brief_link.get_attribute("href")
            assert expected_link in brief_link.get_attribute("href")
            driver.save_screenshot(str(SHOTS / "phase2_review_brief_page.png"))
            report["screenshots"].append("screenshots/phase2_review_brief_page.png")
            brief_link.click()
            report["checks"]["brief_review_page"] = assert_review(driver, wait)

            driver.get(
                f"{BASE}/packs/{PACK_ID}?task_id={PRACTICE_TASK}&view=questions"
            )
            wait.until(lambda d: d.find_element(By.ID, "view-questions").is_displayed())
            wait.until(
                lambda d: "已作答 0 / 4"
                in d.find_element(By.ID, "answered-count").text
            )
            report["checks"]["practice_restored"] = True
            driver.save_screenshot(str(SHOTS / "phase2_review_practice.png"))
            report["screenshots"].append("screenshots/phase2_review_practice.png")
        finally:
            driver.quit()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()