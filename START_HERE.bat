@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title PR MCP Builder

if /I "%~1"=="--check" (
    if exist ".venv\Scripts\python.exe" (
        call "%~dp0RUN_APP.bat" --check
    ) else (
        call "%~dp0INSTALL_AND_RUN.bat" --check
    )
    exit /b
)

echo.
echo ============================================================
echo   PR MCP Builder - Public Institution Regulation MCP Builder
echo ============================================================
echo.

if exist ".venv\Scripts\python.exe" (
    call "%~dp0RUN_APP.bat"
) else (
    echo First run: installing required packages before startup.
    echo Installation requires an internet connection and may take a few minutes.
    echo.
    call "%~dp0INSTALL_AND_RUN.bat"
)

exit /b %ERRORLEVEL%
