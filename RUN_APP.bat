@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title PR MCP Builder - Running

set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "STREAMLIT_BROWSER_GATHER_USAGE_STATS=false"

if not exist "%VENV_PYTHON%" (
    echo [STOPPED] The virtual environment is missing. Run START_HERE.bat first.
    if /I not "%~1"=="--check" pause
    exit /b 1
)

if not exist "frontend\streamlit_app.py" (
    echo [STOPPED] frontend\streamlit_app.py was not found.
    if /I not "%~1"=="--check" pause
    exit /b 1
)

set "APP_PORT="
for /f "delims=" %%P in ('""%VENV_PYTHON%" "%CD%\scripts\find_available_ui_port.py" --preferred 8501"') do set "APP_PORT=%%P"
if not defined APP_PORT (
    echo [STOPPED] An available local application port could not be found.
    if /I not "%~1"=="--check" pause
    exit /b 1
)

if not "%APP_PORT%"=="8501" (
    echo [INFO] Port 8501 is already in use. Using port %APP_PORT% instead.
)

if /I "%~1"=="--check" (
    "%VENV_PYTHON%" -c "import streamlit; print('[OK] Streamlit', streamlit.__version__)"
    echo [OK] Available UI port %APP_PORT%
    exit /b
)

echo.
echo Application URL: http://127.0.0.1:%APP_PORT%
echo Press Ctrl+C in this window to stop the application.
echo.

"%VENV_PYTHON%" -m streamlit run "frontend\streamlit_app.py" --server.address 127.0.0.1 --server.port %APP_PORT% --server.headless false
set "APP_EXIT_CODE=%ERRORLEVEL%"

if not "%APP_EXIT_CODE%"=="0" (
    echo.
    echo [APP STOPPED] The application exited with error code %APP_EXIT_CODE%.
    pause
)

exit /b %APP_EXIT_CODE%
