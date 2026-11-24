from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WINDOW_TITLE = "循知"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
READY_TIMEOUT = 45.0
EXISTING_WAIT = 25.0


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _load_settings():
    from app.config import settings

    return settings


def _log_path(data_dir: Path) -> Path:
    logs = data_dir / "logs"
    try:
        _ensure_dir(logs)
        return logs / "desktop.log"
    except OSError:
        fallback = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Xunzhi"
        _ensure_dir(fallback)
        return fallback / "desktop.log"


def _configure_logging(log_path: Path) -> logging.Logger:
    handlers: list[logging.Handler] = [logging.FileHandler(log_path, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    logger = logging.getLogger("xunzhi.desktop")
    logger.info("desktop launcher starting root=%s log=%s", ROOT, log_path)
    return logger


def _show_error(title: str, message: str, log_path: Path) -> None:
    text = f"{message}\n\n日志位置：\n{log_path}"
    if os.getenv("XUNZHI_DESKTOP_DIAGNOSE") == "1":
        print(f"[xunzhi] {title}: {text}", file=sys.stderr)
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, text, f"循知 - {title}", 0x10)


def _probe_project(host: str, port: int) -> dict | None:
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if payload.get("status") != "ok":
        return None
    if payload.get("app") == "xunzhi":
        return payload
    if payload.get("db") == "ok" and payload.get("migration") is not None:
        return payload
    return None


def _port_in_use(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def _read_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _remove_state(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _acquire_lock(path: Path):
    import msvcrt

    _ensure_dir(path.parent)
    handle = open(path, "a+", encoding="utf-8")
    handle.seek(0)
    handle.write("x")
    handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return handle
    except OSError:
        handle.close()
        return None


def _release_lock(handle) -> None:
    if handle is None:
        return
    import msvcrt

    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass


def _focus_existing_window() -> bool:
    try:
        import ctypes
    except ImportError:
        return False
    hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        return False
    ctypes.windll.user32.ShowWindow(hwnd, 9)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    return True


def _wait_for_existing(host: str, port: int, state_path: Path, timeout: float) -> dict | None:
    state = _read_state(state_path) or {}
    expected = state.get("instance_id")
    deadline = time.time() + timeout
    while time.time() < deadline:
        health = _probe_project(host, port)
        if health is not None and (not expected or health.get("instance_id") == expected):
            return health
        time.sleep(0.4)
    return None


def _check_webview2() -> bool:
    try:
        import winreg
    except ImportError:
        return True
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    )
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as key:
                value, _ = winreg.QueryValueEx(key, "pv")
                if value:
                    return True
        except OSError:
            continue
    return False


def _start_backend(
    host: str,
    port: int,
    instance_id: str,
    data_dir: Path,
    root_dir: Path,
    user_id: str,
):
    env = os.environ.copy()
    env.update(
        {
            "XUNZHI_INSTANCE_ID": instance_id,
            "XUNZHI_HOST": host,
            "XUNZHI_PORT": str(port),
            "XUNZHI_DATA_DIR": str(data_dir),
            "XUNZHI_ROOT_DIR": str(root_dir),
            "XUNZHI_USER_ID": user_id,
            "XUNZHI_DESKTOP": "1",
        }
    )
    log_dir = Path(root_dir) / "logs"
    _ensure_dir(log_dir)
    handle = open(log_dir / "backend.log", "a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "info",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return proc, handle


def _stop_backend(proc, handle) -> None:
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
    try:
        if handle is not None:
            handle.close()
    except Exception:
        pass


def _wait_port_free(host: str, port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_in_use(host, port):
            return True
        time.sleep(0.2)
    return not _port_in_use(host, port)


def _state_payload(host: str, port: int, instance_id: str, user: dict, data_dir: Path, root_dir: Path, log_path: Path) -> dict:
    return {
        "app": "xunzhi",
        "pid": os.getpid(),
        "port": port,
        "instance_id": instance_id,
        "url": f"http://{host}:{port}",
        "owns_service": True,
        "log": str(log_path),
        "root_dir": str(root_dir),
        "data_dir": str(data_dir),
        "user_id": user["id"],
        "user_name": user["display_name"],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _restart_backend(
    host: str,
    port: int,
    root_dir: Path,
    runtime: dict,
    logger: logging.Logger,
    *,
    user: dict,
    data_dir: Path,
) -> str:
    _stop_backend(runtime.get("proc"), runtime.get("handle"))
    runtime["proc"] = None
    runtime["handle"] = None
    runtime["user_id"] = None
    if not _wait_port_free(host, port):
        raise RuntimeError(f"端口 {port} 未被旧服务释放")
    instance_id = uuid.uuid4().hex
    proc, handle = _start_backend(host, port, instance_id, data_dir, root_dir, user["id"])
    runtime.update(
        {
            "proc": proc,
            "handle": handle,
            "instance_id": instance_id,
            "user_id": user["id"],
            "data_dir": str(data_dir),
        }
    )
    if not _wait_ready(host, port, instance_id, READY_TIMEOUT):
        _stop_backend(proc, handle)
        runtime["proc"] = None
        runtime["handle"] = None
        raise RuntimeError("目标工作区服务未在限定时间内就绪")
    return instance_id


def _switch_workspace(
    host: str,
    port: int,
    root_dir: Path,
    runtime: dict,
    request: dict,
    logger: logging.Logger,
    log_path: Path,
) -> dict:
    from app.infra import workspace

    request_id = str(request.get("request_id") or "")
    target = workspace.user_by_id(str(request.get("target_user_id") or ""), root_dir)
    if target is None:
        return {
            "request_id": request_id,
            "status": "failed",
            "message": "目标学习者不存在，已保持原工作区。",
        }
    ok, message = workspace.validate_workspace(target, root_dir)
    if not ok:
        return {
            "request_id": request_id,
            "status": "failed",
            "message": f"{message} 已保持原工作区。",
        }
    previous_user = workspace.user_by_id(str(runtime.get("user_id") or ""), root_dir)
    previous_data = Path(runtime.get("data_dir") or root_dir)
    if previous_user is None:
        previous_user = workspace.current_user(root_dir)
    try:
        workspace.set_current(target["id"], root_dir)
        data_dir = workspace.workspace_path(target, root_dir)
        instance_id = _restart_backend(
            host,
            port,
            root_dir,
            runtime,
            logger,
            user=target,
            data_dir=data_dir,
        )
        state_path = root_dir / ".desktop_state.json"
        _write_state(
            state_path,
            _state_payload(host, port, instance_id, target, data_dir, root_dir, log_path),
        )
        logger.info("workspace switched user=%s workspace=%s", target["id"], data_dir)
        return {
            "request_id": request_id,
            "status": "ok",
            "user_id": target["id"],
            "user_name": target["display_name"],
            "instance_id": instance_id,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspace switch failed target=%s", target.get("id"))
        try:
            workspace.set_current(previous_user["id"], root_dir)
            instance_id = _restart_backend(
                host,
                port,
                root_dir,
                runtime,
                logger,
                user=previous_user,
                data_dir=previous_data,
            )
            state_path = root_dir / ".desktop_state.json"
            _write_state(
                state_path,
                _state_payload(
                    host,
                    port,
                    instance_id,
                    previous_user,
                    previous_data,
                    root_dir,
                    log_path,
                ),
            )
            message = f"切换失败：{exc}；已恢复原工作区。"
        except Exception as restore_exc:  # noqa: BLE001
            logger.exception("workspace restore failed")
            message = f"切换失败且原工作区恢复失败：{restore_exc}。请重新打开桌面版。"
        return {
            "request_id": request_id,
            "status": "failed",
            "message": message,
            "user_id": previous_user["id"],
        }


def _control_loop(
    host: str,
    port: int,
    root_dir: Path,
    runtime: dict,
    logger: logging.Logger,
    log_path: Path,
    stop_event: threading.Event,
) -> None:
    request_path = root_dir / "switch-request.json"
    result_path = root_dir / "switch-result.json"
    while not stop_event.is_set():
        request = _read_state(request_path)
        if request:
            _remove_state(request_path)
            logger.info("switch request received=%s", request.get("request_id"))
            time.sleep(1.0)
            result = _switch_workspace(
                host, port, root_dir, runtime, request, logger, log_path
            )
            _write_state(result_path, result)
            continue
        stop_event.wait(0.5)


def _wait_ready(host: str, port: int, instance_id: str, timeout: float) -> bool:
    from scripts.run_server import wait_until_ready

    return wait_until_ready(host, port, instance_id, timeout=timeout)


def _open_window(url: str, logger: logging.Logger) -> None:
    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "桌面组件 pywebview 未安装。请先运行 setup_desktop.bat 安装桌面依赖。"
        ) from exc

    if not _check_webview2():
        raise RuntimeError(
            "未检测到 Microsoft Edge WebView2 Runtime。请安装 WebView2 Runtime"
            "（https://go.microsoft.com/fwlink/p/?LinkId=2124703）后重新双击桌面「循知」图标。"
        )
    logger.info("opening webview url=%s", url)
    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=1280,
        height=820,
        min_size=(900, 600),
        resizable=True,
        text_select=True,
        confirm_close=False,
    )
    webview.start(gui="edgechromium", private_mode=True, debug=False)


def main() -> int:
    settings = _load_settings()
    from app.infra import workspace

    root_dir = Path(settings.root_dir)
    _ensure_dir(root_dir)
    registry = workspace.ensure_registry(root_dir)
    user = workspace.user_by_id(registry["current_user_id"], root_dir) or next(
        item for item in registry["users"] if item["is_default"]
    )
    data_dir = workspace.workspace_path(user, root_dir)
    _ensure_dir(data_dir)
    log_path = _log_path(root_dir)
    logger = _configure_logging(log_path)
    state_path = root_dir / ".desktop_state.json"
    lock_path = root_dir / ".desktop.lock"
    host = os.getenv("XUNZHI_HOST", settings.host or DEFAULT_HOST)
    port = int(os.getenv("XUNZHI_PORT", settings.port or DEFAULT_PORT))
    instance_id = uuid.uuid4().hex
    lock = _acquire_lock(lock_path)
    owns_service = False
    runtime = {
        "proc": None,
        "handle": None,
        "instance_id": None,
        "user_id": user["id"],
        "data_dir": str(data_dir),
    }
    control_stop = threading.Event()
    control_thread = None

    if lock is None:
        logger.info("another desktop instance holds lock; waiting for existing service")
        health = _wait_for_existing(host, port, state_path, EXISTING_WAIT)
        if health is None:
            message = (
                "已有循知桌面实例正在启动，但服务未在限定时间内就绪。\n"
                "请稍后再双击桌面图标；若持续出现，请关闭旧的循知窗口后重试。"
            )
            _show_error("已有实例未就绪", message, log_path)
            return 1
        logger.info("connected to existing instance instance_id=%s", health.get("instance_id"))
        if _focus_existing_window():
            logger.info("focused existing desktop window")
            return 0
        try:
            _open_window(f"http://{host}:{port}", logger)
        except Exception as exc:  # noqa: BLE001
            logger.exception("webview failed")
            _show_error("窗口打开失败", str(exc), log_path)
            return 1
        return 0

    try:
        existing = _probe_project(host, port)
        if existing is not None:
            logger.info("connected to existing project service instance_id=%s", existing.get("instance_id"))
            _write_state(
                state_path,
                {
                    "app": "xunzhi",
                    "pid": os.getpid(),
                    "port": port,
                    "instance_id": existing.get("instance_id"),
                    "url": f"http://{host}:{port}",
                    "owns_service": False,
                    "log": str(log_path),
                    "root_dir": str(root_dir),
                    "data_dir": str(data_dir),
                    "user_id": existing.get("user_id") or user["id"],
                    "user_name": existing.get("user_name") or user["display_name"],
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
        else:
            if _port_in_use(host, port):
                message = (
                    f"端口 {port} 已被其他程序占用，循知无法启动自带服务。\n"
                    "请关闭占用该端口的程序，或在 .env 中设置其他 XUNZHI_PORT 后重试。"
                )
                logger.error("port occupied port=%s", port)
                _show_error("端口占用", message, log_path)
                return 1
            from scripts.run_server import resolve_port

            try:
                port = resolve_port(host, port)
            except SystemExit as exc:
                _show_error("服务启动失败", str(exc), log_path)
                return 1
            logger.info(
                "starting bundled service host=%s port=%s user=%s workspace=%s",
                host,
                port,
                user["id"],
                data_dir,
            )
            proc, handle = _start_backend(
                host, port, instance_id, data_dir, root_dir, user["id"]
            )
            runtime.update(
                {
                    "proc": proc,
                    "handle": handle,
                    "instance_id": instance_id,
                    "user_id": user["id"],
                    "data_dir": str(data_dir),
                }
            )
            if not _wait_ready(host, port, instance_id, READY_TIMEOUT):
                logger.error("service not ready after %.1fs", READY_TIMEOUT)
                _stop_backend(proc, handle)
                _show_error(
                    "服务启动失败",
                    f"循知服务未在 {int(READY_TIMEOUT)} 秒内通过健康检查。\n"
                    "请查看日志确认端口、数据库或依赖是否正常。",
                    log_path,
                )
                return 1
            owns_service = True
            _write_state(
                state_path,
                _state_payload(host, port, instance_id, user, data_dir, root_dir, log_path),
            )
            control_thread = threading.Thread(
                target=_control_loop,
                args=(host, port, root_dir, runtime, logger, log_path, control_stop),
                name="xunzhi-workspace-control",
                daemon=True,
            )
            control_thread.start()

        try:
            _open_window(f"http://{host}:{port}", logger)
        except Exception as exc:  # noqa: BLE001
            logger.exception("webview failed")
            _show_error("窗口打开失败", str(exc), log_path)
            return 1
        return 0
    finally:
        control_stop.set()
        if control_thread is not None:
            control_thread.join(timeout=2)
        if owns_service and runtime.get("proc") is not None:
            logger.info("window closed; stopping bundled service")
            _stop_backend(runtime.get("proc"), runtime.get("handle"))
            runtime["proc"] = None
            runtime["handle"] = None
        _remove_state(state_path)
        _release_lock(lock)


def _bootstrap() -> int:
    try:
        return main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        fallback = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Xunzhi" / "desktop.log"
        try:
            _ensure_dir(fallback.parent)
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s %(message)s",
                handlers=[logging.FileHandler(fallback, encoding="utf-8")],
                force=True,
            )
            logging.getLogger("xunzhi.desktop").exception("desktop launcher crashed")
        except Exception:
            pass
        _show_error("启动失败", f"{type(exc).__name__}: {exc}", fallback)
        return 1


if __name__ == "__main__":
    raise SystemExit(_bootstrap())