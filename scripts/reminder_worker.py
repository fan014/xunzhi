from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _root() -> Path:
    value = os.getenv("XUNZHI_ROOT_DIR")
    return Path(value) if value else ROOT / "data"


def _read_users(root: Path) -> list[dict]:
    path = root / "users.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data.get("users") or [] if item.get("id")]


def _workspace(root: Path, user: dict) -> Path:
    raw = Path(str(user.get("workspace") or "."))
    return raw if raw.is_absolute() else (root / raw).resolve()


def _run_one(user: dict) -> int:
    root = _root()
    workspace = _workspace(root, user)
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ["XUNZHI_ROOT_DIR"] = str(root)
    os.environ["XUNZHI_DATA_DIR"] = str(workspace)
    os.environ["XUNZHI_USER_ID"] = user["id"]
    os.environ["XUNZHI_DESKTOP"] = "1"

    from app.domain import reminder
    from app.infra import storage
    from app.infra.db import SessionLocal, init_db

    storage.ensure_dirs()
    init_db()
    with SessionLocal() as db:
        status = reminder.run_check(db)
    print(
        json.dumps(
            {
                "user_id": user["id"],
                "state": status.get("state"),
                "task_count": status.get("task_count"),
                "minutes": status.get("minutes"),
                "next_check_at": status.get("next_check_at"),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="循知本机学习提醒检查")
    parser.add_argument("--user-id")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    root = _root()
    users = _read_users(root)
    if args.user_id:
        user = next((item for item in users if item["id"] == args.user_id), None)
        if user is None:
            print(json.dumps({"state": "user_not_found", "user_id": args.user_id}, ensure_ascii=False))
            return 1
        try:
            return _run_one(user)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("xunzhi.reminder").exception("reminder worker failed")
            print(json.dumps({"state": "internal_error", "error": type(exc).__name__}, ensure_ascii=False))
            return 1
    if not users:
        print(json.dumps({"state": "no_users"}, ensure_ascii=False))
        return 0
    failed = 0
    for user in users:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__)), "--user-id", user["id"], "--once"],
            cwd=str(ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())