@echo off
rem Run from source (needs Python 3.10+). First run creates .venv and installs packages.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo First run: creating virtual environment and installing packages...
    python -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)
start "" ".venv\Scripts\pythonw.exe" main.py %*
exit /b 0

:fail
echo.
echo Setup failed. Please make sure Python 3.10+ is installed and on PATH.
pause
exit /b 1
