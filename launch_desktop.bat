@echo off
title PolyTrade Desktop App
cd /d "%~dp0"
echo Starting PolyTrade Desktop App...
venv\Scripts\python.exe desktop_app.py
pause
