@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup_unneeded.ps1"
