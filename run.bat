@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [xunzhi] Python not found. Install Python 3.12 and enable "Add Python to PATH".
  goto :error
)

if not exist ".venv\Scripts\python.exe" (
  echo [xunzhi] creating virtual environment...
  python -m venv .venv
  if errorlevel 1 goto :error
)

echo [xunzhi] installing runtime dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 goto :error

echo [xunzhi] starting; the browser opens only after the health check passes.
".venv\Scripts\python.exe" scripts\run_server.py %*
if errorlevel 1 goto :error
goto :eof

:error
echo [xunzhi] startup failed. See the error above.
pause