@echo off
REM ============================================================
REM   Qyro - start the app (Windows)
REM ============================================================
if not exist .venv (
  echo Please run install.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo.
echo   Qyro is starting... keep this window OPEN.
echo   Open http://localhost:8000 in your browser.
echo   To stop the app, close this window (or press Ctrl+C).
echo.
python run.py --host 127.0.0.1 --port 8000
pause
