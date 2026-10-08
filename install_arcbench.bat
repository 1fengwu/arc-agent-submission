@echo off
REM ============================================================
REM  arcbench-cli installer (run in a NORMAL cmd window with internet)
REM  Steps: clone repo -> create venv -> pip install -e . -> make .env
REM ============================================================
setlocal
cd /d "%~dp0"

set REPO=arcbench-cli
set VENV=.venv

if not exist "%REPO%" (
    echo [1/4] Cloning repo...
    git clone https://github.com/thunderstone-group/arcbench-cli.git
    if errorlevel 1 (
        echo Clone FAILED. Check internet connection and that git is installed.
        pause
        exit /b 1
    )
) else (
    echo [1/4] Repo already exists, skip clone.
)

cd "%REPO%"

echo [2/4] Creating venv...
if not exist "%VENV%" python -m venv %VENV%
call %VENV%\Scripts\activate.bat

echo [3/4] Installing (stdlib only, fast)...
python -m pip install --upgrade pip >nul 2>&1
pip install -e .
if errorlevel 1 (
    echo Install FAILED. Check python version, need 3.10+.
    pause
    exit /b 1
)

echo [4/4] Creating .env from template...
if not exist ".env" (
    copy .env.example .env >nul
    echo .env created. Now open it in Notepad and paste your cookie.
) else (
    echo .env already exists, not overwritten.
)

echo.
echo ============================================================
echo  DONE. Next steps:
echo   1. Edit this file with Notepad:  %cd%\.env
echo      Paste your browser Cookie header after
echo      ARC_BENCH_SESSION_COOKIE=
echo   2. Verify login:     arcbench whoami
echo   3. Hackathon status: arcbench registration hackathon
echo   4. Balance:          arcbench balance
echo ============================================================
call %VENV%\Scripts\activate.bat
arcbench --help
endlocal
pause
