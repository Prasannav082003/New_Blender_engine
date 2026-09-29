@echo off
setlocal
set PYTHON_SCRIPT=C:\Zlendo2026\4k_ai_godot_engine_staging\download_all_assets_new.py

echo ========================================================
echo   🚀 ASSET DOWNLOADER SCHEDULER: STARTING
echo   📅 Waiting for 12:00 AM daily trigger...
echo   (Press Ctrl+C to stop)
echo ========================================================

:LOOP
:: Run the Python script
python "%PYTHON_SCRIPT%"

:: If the script exits or crashes, wait 10 seconds and retry
echo.
echo ⚠️ Python process exited or crashed.
echo 🔄 Restarting scheduler in 10 seconds...
timeout /t 10 /nobreak > nul
goto LOOP
