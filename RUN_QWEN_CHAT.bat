@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONPATH="
set "PYTHONHOME="

title PR MCP - Local Qwen Chat

set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "STREAMLIT_BROWSER_GATHER_USAGE_STATS=false"

if not exist "%VENV_PYTHON%" (
    echo [STOPPED] The virtual environment is missing. Run START_HERE.bat first.
    if /I not "%~1"=="--check" pause
    exit /b 1
)

if not exist "frontend\qwen_chat_app.py" (
    echo [STOPPED] frontend\qwen_chat_app.py was not found.
    if /I not "%~1"=="--check" pause
    exit /b 1
)

"%VENV_PYTHON%" -c "import sys, streamlit, app; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [STOPPED] Python 3.11 or newer and Streamlit are required.
    echo Run START_HERE.bat to repair the local environment.
    if /I not "%~1"=="--check" pause
    exit /b 1
)

if /I "%~1"=="--check" (
    "%VENV_PYTHON%" -m streamlit version
    "%VENV_PYTHON%" -m scripts.run_qwen_chat --help >nul
    if errorlevel 1 exit /b 1
    echo [OK] The standalone localhost Qwen chat launcher is ready.
    exit /b 0
)

echo.
echo Starting the separate localhost Qwen regulation chatbot.
echo The launcher will print the exact local URL below.
echo Press Ctrl+C in this window to stop it.
echo.

"%VENV_PYTHON%" -m scripts.run_qwen_chat %*
set "APP_EXIT_CODE=%ERRORLEVEL%"

if not "%APP_EXIT_CODE%"=="0" (
    echo.
    echo [QWEN CHAT STOPPED] The application exited with error code %APP_EXIT_CODE%.
    pause
)

exit /b %APP_EXIT_CODE%
