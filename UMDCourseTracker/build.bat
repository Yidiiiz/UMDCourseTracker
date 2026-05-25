@echo off
cd /d "%~dp0"
echo ============================================================
echo  UMD Course Tracker - Build
echo ============================================================
echo.

REM Check that PyInstaller is available (use python -m so Scripts\ doesn't need to be in PATH)
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: PyInstaller not found. Run setup.bat first.
    goto :done
)

REM Kill any running instance so the exe isn't locked during build
taskkill /f /im UMDCourseTracker.exe >nul 2>&1

REM Generate icon.ico if it doesn't already exist
if not exist icon.ico (
    echo Generating icon.ico...
    python -c "from PIL import Image, ImageDraw; img=Image.new('RGBA',(64,64),(0,0,0,0)); d=ImageDraw.Draw(img); d.ellipse([4,4,60,60], fill=(34,197,94,255), outline=(255,255,255,200), width=3); img.save('icon.ico', format='ICO', sizes=[(16,16),(32,32),(48,48),(64,64)]); print('icon.ico created')"
    if errorlevel 1 (
        echo ERROR: Failed to generate icon.ico. Is Pillow installed?
        goto :done
    )
)

echo.
echo Building UMDCourseTracker.exe (this may take a minute)...
python -m PyInstaller --onefile --windowed --icon=icon.ico --name=UMDCourseTracker tracker.py

if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. See output above.
    goto :done
)

echo.
echo ============================================================
echo  Build complete!
echo  Executable: dist\UMDCourseTracker.exe
echo.
echo  Copy dist\UMDCourseTracker.exe anywhere you like.
echo  User data (courses.json, settings.json) is stored in:
echo    %%APPDATA%%\UMD Course Tracker\
echo ============================================================

:done
pause
