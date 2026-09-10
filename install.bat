@echo off
REM ============================================================
REM   Qyro - one-click installer (Windows)
REM ============================================================
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  [X] Python was not found on this computer.
  echo.
  echo      1. Install Python 3.10+ from https://www.python.org/downloads/
  echo      2. IMPORTANT: tick "Add Python to PATH" in the installer
  echo      3. Run install.bat again
  echo.
  pause
  exit /b 1
)

echo Creating a private Python environment...
python -m venv .venv
call .venv\Scripts\activate.bat

echo Installing Qyro dependencies (this downloads ffmpeg too)...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo  [X] Installation failed - check your internet connection and try again.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   Installed! Now double-click  run.bat  to start the app.
echo   Then open  http://localhost:8000  in your browser.
echo ============================================================
echo.
pause
