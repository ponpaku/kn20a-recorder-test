@echo off
cd /d "%~dp0"
title KN20A Recorder
py -3 recorder.py
if errorlevel 1 (
  echo.
  echo KN20A Recorder failed to start. See logs\service.log
  pause
)
