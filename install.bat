@echo off
rem SARSense: set up a private Python environment in .venv next to this file.
rem
rem   install.bat               normal install
rem   install.bat --recreate    delete .venv and start again
rem   install.bat --test        also run the test suite
rem   install.bat --no-pause    do not wait for a key at the end (used by the run scripts)
rem   install.bat --desktop     also put a SARSense Hub shortcut on the desktop
rem   install.bat --no-shortcuts        skip the Start menu entries
rem   install.bat --remove-shortcuts    remove the Start menu entries and stop
rem
rem Needs Python 3.9 or newer from https://www.python.org/downloads/
rem (tick "Add python.exe to PATH" or keep the "py" launcher, which is the default).

setlocal EnableExtensions
cd /d "%~dp0"
set "HERE=%~dp0"
set "VENV=%HERE%.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "RECREATE=0"
set "RUNTESTS=0"
set "NOPAUSE=0"
set "SHORTCUTS=1"
set "DESKTOP="
set "PS=powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%tools\windows_shortcuts.ps1" -Root "%HERE%.""

:args
if "%~1"=="" goto args_done
if /i "%~1"=="--recreate" set "RECREATE=1"& shift & goto args
if /i "%~1"=="--test" set "RUNTESTS=1"& shift & goto args
if /i "%~1"=="--no-pause" set "NOPAUSE=1"& shift & goto args
if /i "%~1"=="--no-shortcuts" set "SHORTCUTS=0"& shift & goto args
if /i "%~1"=="--desktop" set "DESKTOP=-Desktop"& shift & goto args
if /i "%~1"=="--remove-shortcuts" goto remove_shortcuts
if /i "%~1"=="--help" goto usage
if /i "%~1"=="-h" goto usage
echo Unknown option: %~1
goto usage
:args_done

rem ------------------------------------------------------------ find Python
set "PYCMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYCMD=py -3"
)
if defined PYCMD goto have_python
rem The Microsoft Store "python" alias fails this check, which is what we want.
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if defined PYCMD goto have_python
echo.
echo Python 3.9 or newer was not found.
echo Install it from https://www.python.org/downloads/ and run install.bat again.
echo If Windows opens the Microsoft Store when you type python, turn off the
echo "App execution aliases" for python in Settings, or use the python.org installer.
goto failed

:have_python
for /f "delims=" %%v in ('%PYCMD% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%v"
echo Using Python %PYVER% (%PYCMD%)

rem ------------------------------------------------------------ create venv
if "%RECREATE%"=="1" if exist "%VENV%\" (
    echo Removing old .venv
    rmdir /s /q "%VENV%"
    if exist "%VENV%\" (
        echo Could not delete .venv. Close any window that is running SARSense and try again.
        goto failed
    )
)

if exist "%VPY%" (
    "%VPY%" -c "import sys" >nul 2>nul
    if errorlevel 1 (
        echo The existing .venv is broken, probably because Python was updated or removed. Recreating it.
        rmdir /s /q "%VENV%"
    )
)

if exist "%VPY%" (
    echo Reusing existing .venv
) else (
    echo Creating .venv
    %PYCMD% -m venv "%VENV%"
    if errorlevel 1 (
        echo Could not create the virtual environment.
        goto failed
    )
)

rem ------------------------------------------------------- install packages
if exist "%HERE%wheels\" (
    echo Installing from the wheels folder ^(offline^)
    "%VPY%" -m pip install --no-index --find-links "%HERE%wheels" -r "%HERE%requirements.txt"
    if errorlevel 1 (
        echo Offline install failed. Check that the wheels folder was made for Python %PYVER% on Windows.
        goto failed
    )
) else (
    echo Updating pip
    "%VPY%" -m pip install --quiet --upgrade pip >nul 2>nul
    echo Installing requirements
    "%VPY%" -m pip install -r "%HERE%requirements.txt"
    if errorlevel 1 (
        echo pip could not install the requirements. Check the internet connection and try again.
        goto failed
    )
)

rem ------------------------------------------------------------------ check
"%VPY%" -c "import numpy, sarsense; print('SARSense %%s ready, NumPy %%s' %% (sarsense.__version__, numpy.__version__))"
if errorlevel 1 (
    echo The environment was created but SARSense does not import. Run install.bat --recreate
    goto failed
)

if "%RUNTESTS%"=="1" (
    echo.
    echo Running tests ^(about 20 seconds^)
    "%VPY%" -m unittest discover -s "%HERE%tests"
    if errorlevel 1 (
        echo Some tests failed, see above.
        goto failed
    )
)

rem ------------------------------------------------------ Start menu entries
if "%SHORTCUTS%"=="1" (
    %PS% %DESKTOP%
    if errorlevel 1 echo Could not create Start menu entries. SARSense still works from this folder.
)

echo.
echo Done. Next:
echo   Start menu, SARSense Demo   try it with simulated sensors (PIN 1234)
echo   Start menu, SARSense Hub    run a real hub
echo   (or run_demo.bat and run_hub.bat in this folder)
echo The web app is then at http://localhost:8080
echo When Windows asks, allow Python through the firewall on private networks
echo so sensors (UDP 5566) and phones (TCP 8080) can reach the hub.
if "%NOPAUSE%"=="0" pause
endlocal & exit /b 0

:usage
echo.
echo Usage: install.bat [options]
echo   --recreate           delete the existing .venv first
echo   --test               run the test suite when done
echo   --desktop            also add a SARSense Hub shortcut to the desktop
echo   --no-shortcuts       do not create Start menu entries
echo   --remove-shortcuts   remove the Start menu entries and stop
echo   --no-pause           do not wait for a key at the end
echo If a "wheels" folder exists, packages are installed from it without internet.
if "%NOPAUSE%"=="0" pause
endlocal & exit /b 2

:remove_shortcuts
%PS% -Remove
if "%NOPAUSE%"=="0" pause
endlocal & exit /b 0

:failed
echo.
echo Install stopped.
if "%NOPAUSE%"=="0" pause
endlocal & exit /b 1
