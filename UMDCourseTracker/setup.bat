@echo off
cd /d "%~dp0"
echo ============================================================
echo  UMD Course Tracker - Setup
echo ============================================================
echo.

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH.
    echo Download Python from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed. Check the output above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Setup complete!
echo  Run:  python tracker.py   to start the tracker
echo  Run:  build.bat           to create a standalone .exe
echo ============================================================
pause
