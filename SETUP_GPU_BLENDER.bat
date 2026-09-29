@echo off
REM Double-click me: downloads the CUDA compiler + OptiX headers into this folder and
REM rebuilds blender.exe with GPU rendering. Progress: gpu_setup.log
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_gpu_blender.ps1"
