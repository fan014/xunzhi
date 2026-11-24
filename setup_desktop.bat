@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [xunzhi] 未找到 .venv，请先运行 run.bat 完成项目依赖安装。
  pause
  exit /b 1
)

echo [xunzhi] 安装桌面依赖（pywebview）...
".venv\Scripts\python.exe" -m pip install -r requirements-desktop.txt --disable-pip-version-check
if errorlevel 1 (
  echo [xunzhi] 桌面依赖安装失败，请检查网络或代理后重试。
  pause
  exit /b 1
)

echo [xunzhi] 创建桌面快捷方式「循知」...
".venv\Scripts\python.exe" scripts\create_desktop_shortcut.py
if errorlevel 1 (
  echo [xunzhi] 快捷方式创建失败。
  pause
  exit /b 1
)

echo [xunzhi] 完成。以后双击桌面「循知」即可打开独立窗口。
pause
