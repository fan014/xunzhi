"""循知启动器：先确认本次实例健康，再打开浏览器。

用法：
  python scripts/run_server.py                 # 8000 端口，就绪后自动开浏览器
  python scripts/run_server.py --no-open       # 只启动，不开浏览器
  python scripts/run_server.py --port 0        # 随机空闲端口（隔离冒烟）
  python scripts/run_server.py --port 8010     # 指定端口

关闭本进程（Ctrl+C 或关闭窗口）即结束服务。端口被占用时会在打开浏览器前明确报错。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HEALTH_TIMEOUT = 30.0


def resolve_port(host: str, requested: int) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, requested))
        port = sock.getsockname()[1]
    except OSError as exc:
        raise SystemExit(
            f"[xunzhi] 端口 {requested} 无法绑定：{exc}\n"
            "请关闭占用该端口的程序，或使用 --port 0 选择空闲端口、--port 8010 指定其他端口。"
        )
    finally:
        sock.close()
    return port


def wait_until_ready(
    host: str, port: int, instance_id: str, timeout: float = HEALTH_TIMEOUT
) -> bool:
    url = f"http://{host}:{port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "ok" and payload.get("instance_id") == instance_id:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def open_when_ready(host: str, port: int, instance_id: str) -> None:
    url = f"http://{host}:{port}"
    if wait_until_ready(host, port, instance_id):
        print(f"[xunzhi] 服务已就绪，打开 {url}")
        webbrowser.open(url)
    else:
        print("[xunzhi] 服务未在 30 秒内就绪，未自动打开浏览器；请查看上方日志。")


def main() -> int:
    parser = argparse.ArgumentParser(description="启动循知本地服务")
    parser.add_argument("--host", default=os.getenv("XUNZHI_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("XUNZHI_PORT", "8000")))
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    port = resolve_port(args.host, args.port)
    instance_id = uuid.uuid4().hex
    os.environ["XUNZHI_INSTANCE_ID"] = instance_id

    import uvicorn

    config = uvicorn.Config(
        "app.main:app",
        host=args.host,
        port=port,
        workers=1,
        log_level="info",
    )
    server = uvicorn.Server(config)
    url = f"http://{args.host}:{port}"
    print(f"[xunzhi] 启动 {url}（实例 {instance_id[:8]}）")

    if not args.no_open:
        threading.Thread(
            target=open_when_ready,
            args=(args.host, port, instance_id),
            name="xunzhi-open-browser",
            daemon=True,
        ).start()

    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())