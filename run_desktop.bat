@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [xunzhi] 未找到 .venv，请先运行 run.bat 或 setup_desktop.bat。
  pause
  exit /b 1
)
echo [xunzhi] 启动桌面版；关闭窗口后本次启动的服务会退出。
".venv\Scripts\python.exe" scripts\desktop_launcher.py %*
if errorlevel 1 (
  echo [xunzhi] 桌面版启动失败，请查看上方错误或 data\logs\desktop.log。
  pause
)
