@echo off
rem Start a SARSense hub using the local .venv (install.bat creates it).
rem Any options are passed to the hub, for example:
rem   run_hub.bat --pin 4821 --hub-id A
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call install.bat --no-pause --no-shortcuts
    if errorlevel 1 (
        pause
        exit /b 1
    )
)
if "%SARSENSE_DATA%"=="" set "SARSENSE_DATA=%~dp0sarsense-data"
set "HASPIN=0"
echo(%* | findstr /i /c:"--pin" /c:"--config" >nul && set "HASPIN=1"
if "%HASPIN%"=="0" if "%SARSENSE_PIN%"=="" set /p "SARSENSE_PIN=Choose an operator PIN (blank for none): "
".venv\Scripts\python.exe" -m sarsense --data-dir "%SARSENSE_DATA%" %*
pause
