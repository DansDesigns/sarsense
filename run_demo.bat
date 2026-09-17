@echo off
rem Starts a hub and the simulator so you can try SARSense without hardware.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call install.bat --no-pause --no-shortcuts
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
start "SARSense hub" ".venv\Scripts\python.exe" -m sarsense --pin 1234 --data-dir "%TEMP%\sarsense-demo"
timeout /t 3 >nul
start "SARSense simulator" ".venv\Scripts\python.exe" tools\simulate.py --setup --pin 1234
start "" http://localhost:8080/
echo The hub and simulator are running in their own windows. Close them to stop.
echo Operator PIN: 1234
