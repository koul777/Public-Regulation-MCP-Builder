@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title PR MCP Builder - Setup

call :find_python
if errorlevel 1 goto :python_missing

if /I "%~1"=="--check" (
    echo [OK] Python 3.11 or newer is available: %PYTHON_CMD%
    exit /b 0
)

echo [1/3] Checking the Python virtual environment.
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

:failed
echo.
echo [SETUP FAILED] Review the error above and run INSTALL_AND_RUN.bat again.
echo.
pause
exit /b 1
