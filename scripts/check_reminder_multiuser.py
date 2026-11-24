from __future__ import annotations

import json
import os
import shutil
import socketserver
import sqlite3
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
SOURCE_DATA = Path("D:/shiyan/xunzhi-phase2-_hc4zu11/data")
DATA_DIR = ROOT / "tests" / ".reminder-multiuser"
B_ID = "user_test_b"
MESSAGES: list[bytes] = []
LOCK = threading.Lock()


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
                    with LOCK:
                        MESSAGES.append(bytes(buffer))
                    buffer.clear()
                    data_mode = False
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
                self.wfile.write(b"354 go\r\n")
            elif command.startswith("QUIT"):
                self.wfile.write(b"221 bye\r\n")
                break
            else:
                self.wfile.write(b"250 OK\r\n")


class SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def prepare_workspace(workspace: Path, task_title: str, email: str, smtp_port: int):
    db_path = workspace / "xunzhi.sqlite3"
    con = sqlite3.connect(db_path)
    row = con.execute(
        "select id from schedule_task where status='pending' order by ordinal limit 1"
    ).fetchone()
    if row is None:
        con.close()
        raise RuntimeError("no pending task")
    today = date.today().isoformat()
    con.execute(
        "update schedule_task set scheduled_date=?, status='pending', task_title=? where id=?",
        (today, task_title, row[0]),
    )
    config = {
        "enabled": True,
        "email": email,
        "time": "00:00",
        "smtp_host": "127.0.0.1",
        "smtp_port": smtp_port,
        "smtp_encryption": "none",
        "smtp_username": "",
    }
    con.execute(
        "insert or replace into app_setting(key,value,updated_at) values(?,?,?)",
        ("settings.reminder", json.dumps(config, ensure_ascii=False), time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    con.execute("delete from app_setting where key like 'reminder.delivery.%'")
    con.commit()
    con.close()
    return row[0]


def run_worker(root: Path, workspace: Path, user_id: str):
    env = os.environ.copy()
    env.update(
        {
            "XUNZHI_ROOT_DIR": str(root),
            "XUNZHI_DATA_DIR": str(workspace),
            "XUNZHI_USER_ID": user_id,
            "XUNZHI_DESKTOP": "1",
            "XUNZHI_ALLOW_FAKE": "1",
        }
    )
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "reminder_worker.py"), "--user-id", user_id, "--once"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def message_info(raw: bytes):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body = ""
    try:
        body = message.get_body(preferencelist=("plain",)).get_content()
    except Exception:
        pass
    return {
        "to": str(message.get("To") or ""),
        "subject": str(message.get("Subject") or ""),
        "body": body,
    }


def main():
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    if not SOURCE_DATA.exists():
        print(json.dumps({"state": "source_missing"}, ensure_ascii=False))
        return
    shutil.copytree(SOURCE_DATA, DATA_DIR)
    b_workspace = DATA_DIR / "users" / B_ID
    shutil.copytree(SOURCE_DATA, b_workspace)
    users = {
        "version": 1,
        "current_user_id": "user_default",
        "users": [
            {
                "id": "user_default",
                "display_name": "A",
                "workspace": ".",
                "is_default": True,
                "created_at": "2026-11-24T00:00:00Z",
            },
            {
                "id": B_ID,
                "display_name": "B",
                "workspace": f"users/{B_ID}",
                "is_default": False,
                "created_at": "2026-11-24T00:00:00Z",
            },
        ],
    }
    (DATA_DIR / "users.json").write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")
    smtp = SMTPServer(("127.0.0.1", 0), SMTPHandler)
    port = smtp.server_address[1]
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    report = {"smtp_port": port}
    try:
        prepare_workspace(DATA_DIR, "A专属阅读任务", "a@example.com", port)
        prepare_workspace(b_workspace, "B专属阅读任务", "b@example.com", port)
        a = run_worker(DATA_DIR, DATA_DIR, "user_default")
        b = run_worker(DATA_DIR, b_workspace, B_ID)
        report["worker_status"] = [a.returncode, b.returncode]
        time.sleep(2)
        infos = [message_info(raw) for raw in MESSAGES]
        report["message_count"] = len(infos)
        report["recipients"] = sorted(info["to"] for info in infos)
        report["a_has_a_task"] = any("A专属阅读任务" in info["body"] and "a@example.com" in info["to"] for info in infos)
        report["b_has_b_task"] = any("B专属阅读任务" in info["body"] and "b@example.com" in info["to"] for info in infos)
        report["a_has_b_task"] = any("B专属阅读任务" in info["body"] and "a@example.com" in info["to"] for info in infos)
        report["b_has_a_task"] = any("A专属阅读任务" in info["body"] and "b@example.com" in info["to"] for info in infos)
        for workspace in (DATA_DIR, b_workspace):
            con = sqlite3.connect(workspace / "xunzhi.sqlite3")
            row = con.execute(
                "select value from app_setting where key='settings.reminder.status'"
            ).fetchone()
            con.close()
            report.setdefault("statuses", []).append(json.loads(row[0]) if row else None)
        assert report["message_count"] == 2
        assert report["a_has_a_task"] and report["b_has_b_task"]
        assert not report["a_has_b_task"] and not report["b_has_a_task"]
    finally:
        smtp.shutdown()
        smtp.server_close()
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()