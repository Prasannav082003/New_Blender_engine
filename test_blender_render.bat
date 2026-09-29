@echo off
setlocal
REM ============================================================
REM  test_blender_render.bat  [input_json]  [output_png]
REM  Double-click works: uses the defaults below.
REM ============================================================
cd /d "%~dp0"
set "BLENDER=%~dp0build_windows_x64_vc18_Release\bin\Release\blender.exe"
set "INPUT=%~1"
if "%INPUT%"=="" set "INPUT=C:\Zlendo2026\Zlendo_render_pt_engine_staging\godot_project\input_json\checking_env_light_4_render_input.json"
set "OUTPUT=%~2"
if "%OUTPUT%"=="" set "OUTPUT=%~dp0output_renders\blender_test.png"

REM quick test settings (half resolution, fewer samples). Remove for full quality.
if "%BLENDER_RESOLUTION_SCALE%"=="" set "BLENDER_RESOLUTION_SCALE=0.5"
if "%BLENDER_SAMPLES%"=="" set "BLENDER_SAMPLES=64"
if "%BLENDER_REQUIRE_GPU%"=="" set "BLENDER_REQUIRE_GPU=false"
set "ASSET_DOWNLOAD_DIR=C:\Zlendo2026\Zlendo_render_engine_staging\asset_downloads"

echo Blender : %BLENDER%
echo Input   : %INPUT%
echo Output  : %OUTPUT%
echo.
if not exist "%BLENDER%" (
  echo ERROR: blender.exe not found at the path above.
  goto :end
)
if not exist "%INPUT%" (
  echo ERROR: input JSON not found at the path above.
  goto :end
)
if not exist "%~dp0output_renders" mkdir "%~dp0output_renders"

"%BLENDER%" --background --factory-startup --python-exit-code 1 --python "%~dp0blender_scripts\render.py" -- "%INPUT%" "%OUTPUT%"
echo.
echo Blender exit code: %ERRORLEVEL%
if exist "%OUTPUT%" (
  echo DONE: %OUTPUT%
  start "" "%OUTPUT%"
) else (
  echo FAILED: no image written. See the error above and output_renders\blender_test_blender_run.log
)

:end
echo.
pause
