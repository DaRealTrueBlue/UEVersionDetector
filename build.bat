@echo off
setlocal

cd /d "%~dp0"

echo [1/2] Installing build dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller

echo [2/2] Building UEVersionDetector executable...
if exist "icon.png" (
    pyinstaller UEVersionDetector.spec
) else (
    pyinstaller --noconfirm --clean --onefile --windowed --name "UEVersionDetector" app.py
)

if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo Build complete. Output located in dist\
exit /b 0
