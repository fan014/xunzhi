from __future__ import annotations

import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from datetime import date
from email import policy
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "tests" / ".reminder-local"
PORT = 8041
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = ROOT / "screenshots"
SHOTS.mkdir(exist_ok=True)
shutil.rmtree(DATA_DIR, ignore_errors=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

os.environ["XUNZHI_DATA_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ROOT_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ALLOW_FAKE"] = "1"
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("XUNZHI_REMINDER_SCHEDULER", None)

import httpx
import uvicorn
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from app.main import app

MESSAGES: list[bytes] = []
MESSAGES_LOCK = threading.Lock()


class SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 localhost\r\n")
        data_mode = False
        buffer = bytearray()
        while True:
            line = self.rfile.readline()
            if not line:
                break
            if data_mode:
                if line.strip() == b".":
                    data_mode = False
                    with MESSAGES_LOCK:
                        MESSAGES.append(bytes(buffer))
                    buffer.clear()
                    self.wfile.write(b"250 OK\r\n")
                else:
                    buffer.extend(line)
                continue
            command = line.decode("utf-8", errors="ignore").strip().upper()
            if command.startswith("EHLO"):
                self.wfile.write(b"250-localhost\r\n250 OK\r\n")
            elif command.startswith("HELO"):
                self.wfile.write(b"250 OK\r\n")
            elif command.startswith("MAIL FROM") or command.startswith("RCPT TO"):
                self.wfile.write(b"250 OK\r\n")
            elif command.startswith("DATA"):
                data_mode = True
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
            elif command.startswith("QUIT"):
                self.wfile.write(b"221 Bye\r\n")
                break
            elif command.startswith("AUTH"):
                self.wfile.write(b"235 Authentication successful\r\n")
            else:
                self.wfile.write(b"250 OK\r\n")


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def wait_health(client: httpx.Client) -> None:
    for _ in range(100):
        try:
            if client.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("server did not start")


def parsed(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


def subject_of(raw: bytes) -> str:
    return str(parsed(raw).get("Subject") or "")


def daily_messages() -> list[bytes]:
    with MESSAGES_LOCK:
        snapshot = list(MESSAGES)
    return [raw for raw in snapshot if "学习提醒" in subject_of(raw)]


def run_worker() -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(
        {
            "XUNZHI_ROOT_DIR": str(DATA_DIR),
            "XUNZHI_DATA_DIR": str(DATA_DIR),
            "XUNZHI_USER_ID": "user_default",
            "XUNZHI_DESKTOP": "1",
        }
    )
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "reminder_worker.py"), "--user-id", "user_default", "--once"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def enable_directly():
    from app.domain import reminder
    from app.infra.db import SessionLocal

    with SessionLocal() as db:
        config = reminder.load_config(db)
        config["enabled"] = True
        reminder.save_config(db, config)


def main():
    smtp = SMTPServer(("127.0.0.1", 0), SMTPHandler)
    smtp_port = smtp.server_address[1]
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    report = {"smtp_port": smtp_port, "checks": {}, "screenshots": []}
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    )
    threading.Thread(target=server.run, daemon=True).start()
    task_name = None
    try:
        from tests.contract.test_schedule_api import _create_plan_pack, _schedule_plan

        with httpx.Client(base_url=BASE, timeout=120) as client:
            wait_health(client)
            plan_id, version_id, _pack_id, _questions = _create_plan_pack(
                client, DATA_DIR, "提醒本地验收", module_count=1
            )
            _schedule_plan(client, plan_id, version_id, start=date.today().isoformat())
            saved = client.put(
                "/api/settings/reminder",
                json={
                    "email": "student@example.com",
                    "time": "00:00",
                    "smtp_host": "127.0.0.1",
                    "smtp_port": smtp_port,
                    "smtp_encryption": "none",
                    "smtp_username": "",
                    "password": None,
                },
            )
            assert saved.status_code == 200, saved.text
            preview = client.post("/api/settings/reminder/preview").json()
            report["checks"]["preview"] = {
                "task_count": preview.get("task_count"),
                "minutes": preview.get("minutes"),
                "has_entry": "打开循知 → 学习日历" in (preview.get("text") or ""),
                "no_private_results": "正确率" not in (preview.get("text") or ""),
            }

            from selenium.webdriver.edge.options import Options as EdgeOptions

            options = EdgeOptions()
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1280,1200")
            driver = webdriver.Edge(options=options)
            wait = WebDriverWait(driver, 20)
            try:
                driver.get(f"{BASE}/settings")
                wait.until(lambda d: d.find_element(By.ID, "settings-reminder").is_displayed())
                card = driver.find_element(By.ID, "settings-reminder")
                driver.execute_script("arguments[0].scrollIntoView({block:'start'})", card)
                driver.save_screenshot(str(SHOTS / "phase3b_reminder_settings.png"))
                report["screenshots"].append("screenshots/phase3b_reminder_settings.png")
                driver.find_element(By.ID, "preview-reminder").click()
                wait.until(lambda d: not d.find_element(By.ID, "reminder-preview-wrap").get_attribute("hidden"))
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'start'})",
                    driver.find_element(By.ID, "reminder-preview-wrap"),
                )
                driver.save_screenshot(str(SHOTS / "phase3b_reminder_preview.png"))
                report["screenshots"].append("screenshots/phase3b_reminder_preview.png")
                driver.find_element(By.ID, "test-reminder").click()
                try:
                    WebDriverWait(driver, 3).until(EC.alert_is_present())
                    driver.switch_to.alert.accept()
                except TimeoutException:
                    pass
                wait.until(lambda d: d.find_element(By.ID, "reminder-test-status").text)
                report["checks"]["ui_test_status"] = driver.find_element(
                    By.ID, "reminder-test-status"
                ).text
                if "已取消" in report["checks"]["ui_test_status"] or "邮件服务" not in report["checks"]["ui_test_status"]:
                    api_test = client.post(
                        "/api/settings/reminder/test",
                        json={"confirm_email": "student@example.com"},
                    )
                    report["checks"]["api_test_status"] = api_test.status_code
                    report["checks"]["api_test_ok"] = (api_test.json().get("result") or {}).get("ok")
                    driver.refresh()
                    wait.until(lambda d: d.find_element(By.ID, "settings-reminder").is_displayed())
                    card = driver.find_element(By.ID, "settings-reminder")
                    driver.execute_script("arguments[0].scrollIntoView({block:'start'})", card)
                driver.save_screenshot(str(SHOTS / "phase3b_reminder_test.png"))
                report["screenshots"].append("screenshots/phase3b_reminder_test.png")
            finally:
                driver.quit()

            with MESSAGES_LOCK:
                test_count = len(MESSAGES)
            report["checks"]["test_message_count"] = test_count
            enabled = client.post("/api/settings/reminder/enable")
            report["checks"]["enable_status"] = enabled.status_code
            report["checks"]["scheduler"] = (
                enabled.json().get("scheduler")
                if enabled.status_code == 200
                else enabled.json()
            )
            if enabled.status_code == 200:
                task_name = (enabled.json().get("scheduler") or {}).get("task_name")
                if task_name:
                    subprocess.run(
                        ["schtasks", "/Run", "/TN", task_name],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
            else:
                enable_directly()

            deadline = time.time() + 60
            while time.time() < deadline and not daily_messages():
                time.sleep(0.5)
            if not daily_messages() and not task_name:
                run_worker()
                time.sleep(2)
            elif not daily_messages() and task_name:
                report["checks"]["scheduler_triggered"] = False
                run_worker()
                time.sleep(2)
            else:
                report["checks"]["scheduler_triggered"] = bool(task_name)

            daily = daily_messages()
            report["checks"]["daily_message_count"] = len(daily)
            if daily:
                body = ""
                try:
                    body = parsed(daily[0]).get_body(preferencelist=("plain",)).get_content()
                except Exception:
                    body = ""
                report["checks"]["daily_subject_ok"] = "学习提醒" in subject_of(daily[0])
                report["checks"]["daily_body_has_entry"] = "学习日历" in body
                report["checks"]["daily_body_has_task"] = "阅读" in body or "练习" in body
            status = client.get("/api/settings/reminder").json()
            report["checks"]["status_after_daily"] = status.get("status", {}).get("state")
            report["checks"]["last_test"] = (status.get("status", {}).get("last_test") or {}).get("state")

            before = len(daily_messages())
            if task_name:
                subprocess.run(
                    ["schtasks", "/Run", "/TN", task_name],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                time.sleep(8)
            else:
                run_worker()
                time.sleep(2)
            report["checks"]["duplicate_daily_count"] = len(daily_messages()) - before

            paused = client.post("/api/settings/reminder/pause")
            report["checks"]["pause_status"] = paused.status_code
            before_paused = len(daily_messages())
            run_worker()
            time.sleep(2)
            report["checks"]["paused_daily_count"] = len(daily_messages()) - before_paused
    finally:
        if task_name:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        smtp.shutdown()
        smtp.server_close()
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()