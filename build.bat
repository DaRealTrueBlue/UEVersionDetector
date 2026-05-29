@echo off
setlocal

cd /d "%~dp0"

echo Building UEVersionDetector executable...
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
