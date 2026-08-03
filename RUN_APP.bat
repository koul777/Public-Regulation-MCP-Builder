@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONPATH="
set "PYTHONHOME="

title PR MCP Builder - Running

set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "STREAMLIT_BROWSER_GATHER_USAGE_STATS=false"

if not exist "%VENV_PYTHON%" (
    echo [STOPPED] The virtual environment is missing. Run START_HERE.bat again.
    if /I not "%~1"=="--check" pause
    exit /b 1
)

call :check_venv_isolation
if errorlevel 1 (
    echo [STOPPED] This setup is not safely isolated from other Python packages.
    echo It will not be changed automatically. Install standard Python 3.11 or newer if needed, then run:
    echo     INSTALL_AND_RUN.bat --recreate-venv
    if /I not "%~1"=="--check" pause
    exit /b 1
)

if not exist "frontend\streamlit_app.py" (
    echo [STOPPED] frontend\streamlit_app.py was not found.
    if /I not "%~1"=="--check" pause
    exit /b 1
)

"%VENV_PYTHON%" -c "from app.utils.fitz_compat import fitz; import sys, pip, streamlit, fastapi, pydantic, pandas, docx, olefile, mcp, kiwipiepy, app; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [STOPPED] The virtual environment needs Python 3.11 or newer with pip.
    echo Run START_HERE.bat to recreate or repair it.
    if /I not "%~1"=="--check" pause
    exit /b 1
)
"%VENV_PYTHON%" -m pip check >nul 2>&1
if errorlevel 1 (
    echo [STOPPED] Installed packages in the virtual environment are inconsistent.
    echo Run START_HERE.bat to repair them.
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
    "%VENV_PYTHON%" -m streamlit version
    echo [OK] Available UI port: %APP_PORT%
    exit /b 0
)

echo.
echo Application URL: http://127.0.0.1:%APP_PORT%
echo If the browser does not open, copy the URL above into the browser address bar.
echo Press Ctrl+C in this window to stop the application.
echo.

"%VENV_PYTHON%" -m streamlit run "frontend\streamlit_app.py" --server.address 127.0.0.1 --server.port %APP_PORT% --server.headless false
set "APP_EXIT_CODE=%ERRORLEVEL%"

if not "%APP_EXIT_CODE%"=="0" (
    echo.
    echo [APP STOPPED] The application exited with error code %APP_EXIT_CODE%.
    echo Run START_HERE.bat again to detect and repair an incomplete setup.
    pause
)

exit /b %APP_EXIT_CODE%

:check_venv_isolation
"%VENV_PYTHON%" -I "%~dp0scripts\check_build_environment_isolation.py" --venv-root "%CD%\.venv" --fail-on-issue >nul 2>&1
exit /b %ERRORLEVEL%
