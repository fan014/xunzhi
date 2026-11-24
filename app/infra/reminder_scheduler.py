from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("xunzhi.reminder.scheduler")

TASK_PREFIX = "XunzhiReminder_"
CREATE_NO_WINDOW = 0x08000000


def task_name(user_id: str) -> str:
    safe = "".join(ch for ch in str(user_id) if ch.isalnum() or ch in "_-")
    return f"{TASK_PREFIX}{safe}"


def _python_executable() -> str:
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else Path(sys.executable))


def _script_path(root: Path) -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "reminder_worker.py"


def _write_runner(root: Path, user: dict, workspace: Path) -> Path:
    runner_dir = Path(root) / "reminders"
    runner_dir.mkdir(parents=True, exist_ok=True)
    runner = runner_dir / f"run_{task_name(user['id'])}.cmd"
    log_path = Path(root) / "logs" / f"reminder_{user['id']}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    python = _python_executable()
    worker = _script_path(root)
    content = (
        "@echo off\r\n"
        f'set "XUNZHI_ROOT_DIR={root}"\r\n'
        f'set "XUNZHI_DATA_DIR={workspace}"\r\n'
        f'set "XUNZHI_USER_ID={user["id"]}"\r\n'
        'set "XUNZHI_DESKTOP=1"\r\n'
        f'"{python}" "{worker}" --user-id {user["id"]} --once '
        f'>> "{log_path}" 2>&1\r\n'
    )
    runner.write_text(content, encoding="utf-8")
    return runner


def register(root: Path, user: dict, workspace: Path) -> dict:
    if os.getenv("XUNZHI_REMINDER_SCHEDULER", "").lower() == "off":
        return {"ok": False, "code": "scheduler_disabled", "message": "调度器已通过配置关闭。"}
    if os.name != "nt":
        return {"ok": False, "code": "scheduler_unsupported", "message": "当前系统不支持 Windows 任务计划程序。"}
    try:
        runner = _write_runner(root, user, workspace)
    except OSError as exc:
        return {"ok": False, "code": "scheduler_runner_failed", "message": f"无法写入本机调度脚本：{exc}"}
    name = task_name(user["id"])
    command = [
        "schtasks",
        "/Create",
        "/TN",
        name,
        "/SC",
        "MINUTE",
        "/MO",
        "5",
        "/TR",
        f'"{runner}"',
        "/F",
        "/RL",
        "LIMITED",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("reminder scheduler register failed code=%s", type(exc).__name__)
        return {"ok": False, "code": "scheduler_failed", "message": f"注册本机调度失败：{exc}"}
    if completed.returncode != 0:
        logger.warning("reminder scheduler register rejected code=%s", completed.returncode)
        return {
            "ok": False,
            "code": "scheduler_failed",
            "message": "Windows 任务计划程序注册失败；请在桌面版设置页查看说明或以当前用户重建任务。",
            "returncode": completed.returncode,
        }
    return {
        "ok": True,
        "code": "scheduler_registered",
        "message": f"已注册 Windows 任务计划程序 {name}，每 5 分钟检查一次。",
        "task_name": name,
    }


def unregister(user_id: str) -> dict:
    if os.getenv("XUNZHI_REMINDER_SCHEDULER", "").lower() == "off":
        return {"ok": False, "code": "scheduler_disabled", "message": "调度器已通过配置关闭。"}
    if os.name != "nt":
        return {"ok": False, "code": "scheduler_unsupported", "message": "当前系统不支持 Windows 任务计划程序。"}
    name = task_name(user_id)
    try:
        completed = subprocess.run(
            ["schtasks", "/Delete", "/TN", name, "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "scheduler_failed", "message": f"移除本机调度失败：{exc}"}
    if completed.returncode != 0:
        return {
            "ok": False,
            "code": "scheduler_missing",
            "message": "未找到或无法移除既有的 Windows 任务；提醒已暂停，任务触发时会读取暂停状态并跳过。",
        }
    return {"ok": True, "code": "scheduler_removed", "message": "已移除 Windows 学习提醒任务。"}


def status(user_id: str) -> dict:
    if os.name != "nt":
        return {"state": "unsupported", "message": "当前系统不支持 Windows 任务计划程序。"}
    name = task_name(user_id)
    try:
        completed = subprocess.run(
            ["schtasks", "/Query", "/TN", name],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return {"state": "unknown", "message": "无法查询 Windows 任务计划程序。"}
    if completed.returncode == 0:
        return {"state": "registered", "message": f"任务 {name} 已注册。"}
    return {"state": "missing", "message": f"任务 {name} 尚未注册。"}