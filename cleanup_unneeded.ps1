# ============================================================================
#  cleanup_unneeded.ps1 - removes files the Blender pipeline no longer needs.
#  Shows the list + sizes first and only deletes after you type Y.
#  (The Godot .gd originals stay available in
#   C:\Zlendo2026\Zlendo_render_pt_engine_staging\godot_project)
# ============================================================================
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$targets = @(
  # --- Godot scripts / scenes (replaced by blender_scripts\*.py) ---
  "blender_scripts\compositor_effect.gd",
  "blender_scripts\custom_shape.gd",
  "blender_scripts\image_glb_creation.gd",
  "blender_scripts\mo_image.gd",
  "blender_scripts\mo_video.gd",
  "blender_scripts\render.gd",
  "blender_scripts\renderer.gd",
  "blender_scripts\structure.gd",
  "blender_scripts\video_glb_creation.gd",
  "blender_scripts\video_render.gd",
  "blender_scripts\main.tscn",
  "blender_scripts\video_main.tscn",
  "blender_scripts\project.godot",
  "blender_scripts\day_CITY.exr",            # unused sky (day.exr is kept as fallback sky)
  "blender_scripts\__pycache__",
  # --- GPU build leftovers (Blender is rebuilt with OptiX/CUDA now) ---
  "_gpu_downloads",                           # ~0.9 GB of installer zips
  "cuda_12.9",                                # failed CUDA version (13.4.2 is the one used)
  "build_blender_gpu.bat",                    # superseded by SETUP_GPU_BLENDER.bat
  "gpu_setup.log",
  # --- old backups / logs ---
  "main_before_blender_backend.py",           # original Godot main.py (also in the staging folder)
  "validation.log",
  "__pycache__"
)

$found = @()
foreach ($t in $targets) {
  if (Test-Path $t) {
    $size = (Get-ChildItem $t -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    $found += [pscustomobject]@{ Path = $t; MB = [math]::Round($size / 1MB, 1) }
  }
}
if (-not $found) { Write-Host "Nothing to clean."; Read-Host "Press Enter"; exit }
$found | Format-Table -AutoSize
Write-Host ("Total: {0:N1} MB" -f (($found | Measure-Object MB -Sum).Sum))
Write-Host ""
Write-Host "KEPT on purpose: blender_scripts\*.py, blender_scripts\day.exr, build_windows_x64_vc18_Release,"
Write-Host "  blender (source), cuda_13.4.2 + optix_sdk + SETUP_GPU_BLENDER.* (only needed to rebuild Blender),"
Write-Host "  app, zlendo_llm, tools, venv, main.py, scene_optimizer*.py, .env, godot_logs (main.py writes logs there)"
$ans = Read-Host "Delete the files above? (Y/N)"
if ($ans -ne "Y" -and $ans -ne "y") { Write-Host "Cancelled."; exit }
foreach ($f in $found) {
  Remove-Item $f.Path -Recurse -Force -ErrorAction Continue
  Write-Host "deleted $($f.Path)"
}
Read-Host "Done. Press Enter"
