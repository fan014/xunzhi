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
DATA_DIR = ROOT / "tests" / ".settings-tabs"
PORT = 8042
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = ROOT / "screenshots"
SHOTS.mkdir(exist_ok=True)

shutil.rmtree(DATA_DIR, ignore_errors=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["XUNZHI_DATA_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ROOT_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ALLOW_FAKE"] = "1"
os.environ.pop("DEEPSEEK_API_KEY", None)

import httpx
import uvicorn
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from app.main import app


def wait_health(client):
    for _ in range(80):
        try:
            if client.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("server did not start")


def visible(driver, panel_id):
    return driver.execute_script(
        f"return !document.getElementById('{panel_id}').hidden"
    )


def main():
    report = {"checks": {}, "screenshots": []}
    thread = threading.Thread(
        target=lambda: uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
        ).run(),
        daemon=True,
    )
    thread.start()
    with httpx.Client(base_url=BASE, timeout=30) as client:
        wait_health(client)
        client.put(
            "/api/settings/appearance",
            json={"theme": "dark", "font_size": "large", "font_family": "reading"},
        )
    from selenium.webdriver.edge.options import Options as EdgeOptions

    options = EdgeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,1000")
    driver = webdriver.Edge(options=options)
    wait = WebDriverWait(driver, 20)
    try:
        driver.get(f"{BASE}/settings")
        wait.until(lambda d: d.find_element(By.ID, "settings-person").is_displayed())
        report["checks"]["default_person"] = visible(driver, "settings-person")
        report["checks"]["default_model_hidden"] = not visible(driver, "settings-model")
        report["checks"]["hash_default"] = driver.execute_script("return location.hash")
        driver.save_screenshot(str(SHOTS / "phase3a_settings_tabs_light.png"))
        report["screenshots"].append("screenshots/phase3a_settings_tabs_light.png")

        driver.find_element(By.CSS_SELECTOR, '[data-settings-link="settings-model"]').click()
        wait.until(lambda d: visible(d, "settings-model"))
        report["checks"]["model_only"] = (
            visible(driver, "settings-model")
            and not visible(driver, "settings-person")
            and not visible(driver, "settings-reminder")
        )
        report["checks"]["hash_model"] = driver.execute_script("return location.hash")

        driver.find_element(By.CSS_SELECTOR, '[data-settings-link="settings-person"]').click()
        wait.until(lambda d: visible(d, "settings-person"))
        name = driver.find_element(By.ID, "display-name")
        name.clear()
        name.send_keys("未保存显示名")
        driver.find_element(By.CSS_SELECTOR, '[data-settings-link="settings-appearance"]').click()
        wait.until(lambda d: visible(d, "settings-appearance"))
        driver.find_element(By.CSS_SELECTOR, '[data-settings-link="settings-person"]').click()
        wait.until(lambda d: visible(d, "settings-person"))
        report["checks"]["preserved_unsaved"] = driver.find_element(By.ID, "display-name").get_attribute("value")

        driver.get(f"{BASE}/settings#settings-reminder")
        wait.until(lambda d: visible(d, "settings-reminder"))
        report["checks"]["direct_reminder"] = visible(driver, "settings-reminder")
        report["checks"]["direct_reminder_only"] = (
            visible(driver, "settings-reminder")
            and not visible(driver, "settings-model")
            and not visible(driver, "settings-person")
        )
        report["checks"]["hash_reminder"] = driver.execute_script("return location.hash")
        driver.save_screenshot(str(SHOTS / "phase3a_settings_reminder_direct.png"))
        report["screenshots"].append("screenshots/phase3a_settings_reminder_direct.png")

        driver.back()
        wait.until(lambda d: visible(d, "settings-person") or visible(d, "settings-appearance"))
        report["checks"]["back_hash"] = driver.execute_script("return location.hash")
        driver.forward()
        wait.until(lambda d: visible(d, "settings-reminder"))
        report["checks"]["forward_hash"] = driver.execute_script("return location.hash")

        driver.set_window_size(390, 900)
        driver.get(f"{BASE}/settings#settings-model")
        wait.until(lambda d: visible(d, "settings-model"))
        report["checks"]["narrow_model"] = visible(driver, "settings-model")
        report["checks"]["narrow_tabs_visible"] = driver.find_element(
            By.CSS_SELECTOR, ".settings-tabs"
        ).is_displayed()
        driver.save_screenshot(str(SHOTS / "phase3a_settings_tabs_390.png"))
        report["screenshots"].append("screenshots/phase3a_settings_tabs_390.png")
    finally:
        driver.quit()
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()