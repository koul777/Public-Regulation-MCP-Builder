@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONPATH="
set "PYTHONHOME="

title PR MCP Builder

if /I "%~1"=="--check" goto :check

echo.
echo ============================================================
echo   PR MCP Builder - Public Institution Regulation MCP Builder
echo ============================================================
echo.

set "VENV_READY="
set "VENV_PYTHON_WORKS="
set "VENV_ISOLATED="
set "VENV_ISOLATION_FAILED="
if exist ".venv\Scripts\python.exe" (
    call :check_venv_isolation
    if errorlevel 1 (
        set "VENV_ISOLATION_FAILED=1"
    ) else (
        set "VENV_ISOLATED=1"
        ".venv\Scripts\python.exe" -c "import sys, pip; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
        if not errorlevel 1 set "VENV_PYTHON_WORKS=1"
    )
)

if defined VENV_ISOLATION_FAILED (
    echo [STOPPED] The previous setup is not safely isolated from other Python packages.
    echo It will not be replaced automatically.
    echo Install standard Python 3.11 or newer if needed, then run:
    echo     INSTALL_AND_RUN.bat --recreate-venv
    exit /b 1
)
if defined VENV_PYTHON_WORKS (
    ".venv\Scripts\python.exe" -c "from app.utils.fitz_compat import fitz; import streamlit, fastapi, pydantic, pandas, docx, olefile, mcp, kiwipiepy, app" >nul 2>&1
    if not errorlevel 1 (
        ".venv\Scripts\python.exe" -m pip check >nul 2>&1
        if not errorlevel 1 set "VENV_READY=1"
    )
)

if defined VENV_READY (
    call "%~dp0RUN_APP.bat"
) else (
    echo Installation requires an internet connection and may take a few minutes.
    echo.
    if defined VENV_PYTHON_WORKS (
        echo The previous setup did not finish. Repairing required packages now.
        call "%~dp0INSTALL_AND_RUN.bat"
    ) else if exist ".venv\Scripts\python.exe" (
        echo The previous virtual environment is damaged. Recreating it now.
        call "%~dp0INSTALL_AND_RUN.bat" --recreate-venv
    ) else (
        echo First run: installing required packages before startup.
        call "%~dp0INSTALL_AND_RUN.bat"
    )
    if errorlevel 1 exit /b 1
)

if errorlevel 1 exit /b 1
exit /b 0

:check
if exist ".venv\Scripts\python.exe" (
    call "%~dp0RUN_APP.bat" --check
) else (
    call "%~dp0INSTALL_AND_RUN.bat" --check
)
exit /b %ERRORLEVEL%

:check_venv_isolation
if not exist ".venv\Scripts\python.exe" exit /b 1
".venv\Scripts\python.exe" -I "%~dp0scripts\check_build_environment_isolation.py" --venv-root "%CD%\.venv" --fail-on-issue >nul 2>&1
exit /b %ERRORLEVEL%
