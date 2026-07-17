@echo off
REM launch.bat — double-clickable entry point for TBH launcher.
REM Uses `python` (not `py`) because `py` is not on PATH on the botting PC.
cd /d "%~dp0"
start "" pythonw.exe tbh_launcher.py
