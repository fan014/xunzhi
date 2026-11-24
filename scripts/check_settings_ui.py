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
DATA_DIR = ROOT / "tests" / ".settings-ui"
SHOTS = ROOT / "screenshots"
PORT = 8036
BASE = f"http://127.0.0.1:{PORT}"

shutil.rmtree(DATA_DIR, ignore_errors=True)
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
    with httpx.Client(base_url=BASE, timeout=60) as client:
        wait_health(client)
        from selenium.webdriver.edge.options import Options as EdgeOptions

        options = EdgeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,900")
        driver = webdriver.Edge(options=options)
        wait = WebDriverWait(driver, 20)
        try:
            driver.get(f"{BASE}/settings")
            wait.until(lambda d: d.find_element(By.ID, "current-user-name").text)
            assert driver.find_element(By.ID, "current-user-name").text == "本机用户"
            assert "设置" in driver.find_element(By.CSS_SELECTOR, ".side-foot").text
            report["checks"]["sidebar_settings"] = True
            driver.save_screenshot(str(SHOTS / "phase3_settings_light.png"))
            report["screenshots"].append("screenshots/phase3_settings_light.png")

            driver.find_element(By.CSS_SELECTOR, 'input[name="theme"][value="dark"]').click()
            driver.find_element(By.CSS_SELECTOR, 'input[name="font_size"][value="large"]').click()
            driver.find_element(By.CSS_SELECTOR, 'input[name="font_family"][value="reading"]').click()
            wait.until(lambda d: "preview_theme=dark" in d.find_element(By.ID, "theme-style").get_attribute("href"))
            wait.until(
                lambda d: d.execute_script(
                    "return getComputedStyle(document.body).backgroundColor"
                )
                == "rgb(17, 22, 29)"
            )
            report["checks"]["preview_dark"] = True
            report["checks"]["preview_font_size"] = driver.execute_script(
                "return getComputedStyle(document.body).fontSize"
            )
            driver.find_element(By.ID, "save-appearance").click()
            wait.until(lambda d: "已保存" in d.find_element(By.ID, "appearance-status").text)
            report["checks"]["appearance_saved"] = True
            driver.save_screenshot(str(SHOTS / "phase3_settings_dark.png"))
            report["screenshots"].append("screenshots/phase3_settings_dark.png")

            driver.get(f"{BASE}/")
            wait.until(lambda d: d.find_element(By.ID, "main").is_displayed())
            wait.until(
                lambda d: d.execute_script(
                    "return getComputedStyle(document.body).backgroundColor"
                )
                == "rgb(17, 22, 29)"
            )
            report["checks"]["home_dark"] = True
            report["checks"]["home_font_size"] = driver.execute_script(
                "return getComputedStyle(document.body).fontSize"
            )
            driver.save_screenshot(str(SHOTS / "phase3_home_dark.png"))
            report["screenshots"].append("screenshots/phase3_home_dark.png")

            driver.get(f"{BASE}/calendar")
            wait.until(lambda d: d.find_element(By.ID, "calendar-grid").is_displayed())
            report["checks"]["calendar_dark"] = (
                driver.execute_script("return getComputedStyle(document.body).backgroundColor")
                == "rgb(17, 22, 29)"
            )
            driver.save_screenshot(str(SHOTS / "phase3_calendar_dark.png"))
            report["screenshots"].append("screenshots/phase3_calendar_dark.png")

            driver.set_window_size(390, 844)
            driver.get(f"{BASE}/settings")
            wait.until(lambda d: d.find_element(By.ID, "settings-person-title").is_displayed())
            report["checks"]["narrow_settings"] = True
            driver.save_screenshot(str(SHOTS / "phase3_settings_390.png"))
            report["screenshots"].append("screenshots/phase3_settings_390.png")

            driver.set_window_size(1280, 900)
            driver.get(f"{BASE}/settings")
            wait.until(lambda d: d.find_element(By.ID, "open-create-user").is_displayed())
            driver.find_element(By.ID, "open-create-user").click()
            driver.find_element(By.ID, "new-user-name").send_keys("网页学习者 B")
            driver.find_element(By.ID, "create-user").click()
            wait.until(
                lambda d: len(
                    d.find_element(By.ID, "user-select").find_elements(By.TAG_NAME, "option")
                )
                >= 2
            )
            report["checks"]["created_user"] = True
            report["checks"]["switch_note"] = driver.find_element(By.ID, "switch-note").text
            report["checks"]["switch_disabled"] = driver.find_element(
                By.ID, "switch-user"
            ).get_attribute("disabled")
            driver.save_screenshot(str(SHOTS / "phase3_settings_users.png"))
            report["screenshots"].append("screenshots/phase3_settings_users.png")

            driver.find_element(By.ID, "test-model").click()
            wait.until(lambda d: d.find_element(By.ID, "model-test-status").text)
            report["checks"]["model_test_without_key"] = driver.find_element(
                By.ID, "model-test-status"
            ).text
        finally:
            driver.quit()
    report["api"] = {
        "appearance": httpx.get(f"{BASE}/api/settings/appearance", timeout=10).json(),
        "identity": httpx.get(f"{BASE}/api/settings/identity", timeout=10).json(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    shutil.rmtree(DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()