@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONPATH="
set "PYTHONHOME="

title PR MCP Builder - Setup

set "RECREATE_VENV="
if /I "%~1"=="--recreate-venv" set "RECREATE_VENV=1"

call :find_python
if errorlevel 1 goto :python_missing

if /I "%~1"=="--check" (
    echo [OK] Python 3.11 or newer is available: %PYTHON_CMD%
    exit /b 0
)

echo [1/3] Checking the Python virtual environment.
if defined RECREATE_VENV (
    call :verify_recreate_target
    if errorlevel 1 goto :venv_recreate_refused
    if exist ".venv" (
        echo Removing only the selected virtual environment before repair.
        rmdir /s /q ".venv"
        if exist ".venv" goto :venv_repair_failed
    )
)
if not defined RECREATE_VENV if exist ".venv" if not exist ".venv\Scripts\python.exe" (
    echo [STOPPED] The previous virtual environment is incomplete.
    echo It will not be removed automatically. To recreate only this project's environment, run:
    echo     INSTALL_AND_RUN.bat --recreate-venv
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv ".venv"
    if errorlevel 1 goto :failed
)
if not exist ".venv\Scripts\python.exe" goto :venv_missing

echo [2/3] Installing required Python packages.
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto :failed

call :check_venv_isolation
if errorlevel 1 goto :isolation_failed

echo [3/3] Setup is complete. Starting the application.
call "%~dp0RUN_APP.bat"
exit /b %ERRORLEVEL%

:find_python
set "PYTHON_CMD="
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3.11"

if not defined PYTHON_CMD (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD exit /b 1
exit /b 0

:python_missing
echo.
echo [STOPPED] Python 3.11 or newer was not found.
echo.
echo If the Python install manager is available, open PowerShell and run:
echo     py install 3.11
echo.
echo Otherwise install Python from:
echo     https://www.python.org/downloads/windows/
echo Select "Add python.exe to PATH" during Python setup.
echo Open a new terminal after installation, then run START_HERE.bat again.
echo.
pause
exit /b 1

:venv_missing
echo.
echo [SETUP FAILED] Python did not create .venv\Scripts\python.exe.
echo The selected command was: %PYTHON_CMD%
echo Install or repair Python 3.11 or newer, then run START_HERE.bat again.
echo.
pause
exit /b 1

:venv_repair_failed
echo.
echo [SETUP FAILED] The damaged .venv folder could not be removed.
echo Close programs using this project, then run START_HERE.bat again.
echo.
pause
exit /b 1

:venv_recreate_refused
echo.
echo [STOPPED] The requested virtual-environment folder was not safe to remove.
echo Only this project's normal .venv folder can be recreated.
echo.
pause
exit /b 1

:isolation_failed
echo.
echo [STOPPED] Setup stopped because this virtual environment is using packages outside itself.
echo It will not retry automatically.
echo If only Anaconda Python is installed, install standard Python 3.11 or newer first.
echo Then run: INSTALL_AND_RUN.bat --recreate-venv
echo.
pause
exit /b 1

:check_venv_isolation
if not exist ".venv\Scripts\python.exe" exit /b 1
".venv\Scripts\python.exe" -I "%~dp0scripts\check_build_environment_isolation.py" --venv-root "%CD%\.venv" --fail-on-issue >nul 2>&1
exit /b %ERRORLEVEL%

:verify_recreate_target
%PYTHON_CMD% "%~dp0scripts\check_windows_venv_recreate_target.py" --path "%CD%\.venv" >nul 2>&1
exit /b %ERRORLEVEL%

:failed
echo.
echo [SETUP FAILED] Review the error above, then run START_HERE.bat again.
echo.
pause
exit /b 1
