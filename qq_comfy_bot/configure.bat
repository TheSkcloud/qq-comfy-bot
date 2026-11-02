@echo off
cd /d "%~dp0"
python configure.py
if errorlevel 1 pause
