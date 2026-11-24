@echo off
rem Double-click this file to start the AI Support Ticket Copilot demo.
rem It runs launcher.py with the project's .venv: the app starts on http://127.0.0.1:8080
rem and opens in your browser. Close this window (or press Ctrl+C) to stop the app.

title AI Support Ticket Copilot
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Can't find the .venv folder in %~dp0
    echo Follow "Setup" in README.md once, then double-click this file again.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" launcher.py
rem Keep the window open after a problem, so the message can be read.
if errorlevel 1 pause
