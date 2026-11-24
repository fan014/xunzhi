from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHORTCUT_NAME = "循知.lnk"


def _powershell() -> str:
    return "powershell.exe"


def _quote(value: str) -> str:
    return value.replace("'", "''")


def main() -> int:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        pythonw = ROOT / ".venv" / "Scripts" / "python.exe"
    if not pythonw.exists():
        print("[xunzhi] 未找到 .venv\\Scripts\\pythonw.exe，请先运行 run.bat 或 setup_desktop.bat。")
        return 1
    launcher = ROOT / "scripts" / "desktop_launcher.py"
    icon = ROOT / "assets" / "xunzhi.ico"
    if not launcher.exists():
        print(f"[xunzhi] 启动器不存在：{launcher}")
        return 1
    icon_value = f"{icon},0" if icon.exists() else f"{pythonw},0"
    script = f"""
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop '{SHORTCUT_NAME}'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = '{_quote(str(pythonw))}'
$shortcut.Arguments = '"{_quote(str(launcher))}"'
$shortcut.WorkingDirectory = '{_quote(str(ROOT))}'
$shortcut.WindowStyle = 1
$shortcut.Description = '循知 - 本机学习助手'
$shortcut.IconLocation = '{_quote(icon_value)}'
$shortcut.Save()
Write-Output $shortcutPath
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        print("[xunzhi] 创建桌面快捷方式失败：")
        print(completed.stdout)
        print(completed.stderr)
        return completed.returncode
    print("[xunzhi] 已创建桌面快捷方式：")
    print(completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else SHORTCUT_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())