@echo off
setlocal
set "TASK_PYTHON=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%TASK_PYTHON%" set "TASK_PYTHON=%~dp0packaging-env\Scripts\pythonw.exe"
if not exist "%TASK_PYTHON%" (
    echo The Python environment is missing. Follow the setup instructions in README.md.
    pause
    exit /b 1
)
start "" "%TASK_PYTHON%" "%~dp0Seraphine\main.py"
