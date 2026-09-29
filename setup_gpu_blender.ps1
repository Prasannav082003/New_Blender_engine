# ============================================================================
#  setup_gpu_blender.ps1  -  give the self-built Blender GPU (CUDA + OptiX) rendering.
#
#  1. Reads the GPU + driver from nvidia-smi.
#  2. Downloads the CUDA 12.9 compiler (nvcc + headers) as NVIDIA's official
#     redistributable zips into  Blender_engine\cuda_12.9   (no installer, no admin,
#     does NOT touch the display driver).
#  3. Downloads the OptiX SDK headers (NVIDIA/optix-dev on GitHub) into
#     Blender_engine\optix_sdk  (version chosen from the driver).
#  4. Re-configures build_windows_x64_vc18_Release with GPU kernels for THIS GPU only
#     and rebuilds (INSTALL) inside the Visual Studio x64 environment.
#  5. Checks that Blender now lists the GPU.
#  Everything is logged to Blender_engine\gpu_setup.log
# ============================================================================
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Src     = Join-Path $Root "blender"
$Build   = Join-Path $Root "build_windows_x64_vc18_Release"
$Log     = Join-Path $Root "gpu_setup.log"
$CudaVer = ""        # resolved below: newest CUDA 13.x (VS 2026 / MSVC 14.5x needs CUDA 13)
$CudaDir = ""
$OptixDir= Join-Path $Root "optix_sdk"
$Dl      = Join-Path $Root "_gpu_downloads"

function Say($m) { $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m; Write-Host $line -ForegroundColor Cyan; Add-Content -Path $Log -Value $line }
function RunCmd($line) {
    # Runs through cmd.exe and merges stderr INSIDE cmd, so PowerShell only ever sees stdout
    # (Windows PowerShell 5.1 turns native stderr lines - e.g. CMake warnings - into errors).
    Say "RUN: $line"
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    & cmd.exe /c "$line 2>&1" | ForEach-Object { $s = "$_"; Write-Host $s; Add-Content -Path $Log -Value $s }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -ne 0) { throw "command failed with exit code $code : $line" }
}
function Run($exe, $argList) {
    Say "RUN: $exe $argList"
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"   # native stderr must not abort
    & $exe @argList 2>&1 | ForEach-Object { $s = "$_"; Write-Host $s; Add-Content -Path $Log -Value $s }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -ne 0) { throw "$exe exited with code $code" }
}

Set-Content -Path $Log -Value "=== GPU setup started $(Get-Date) ==="
try {
    # ------------------------------------------------------------------ 1. GPU
    Say "1/5 Detecting NVIDIA GPU"
    $smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
    if (-not $smi) { $smi = "C:\Windows\System32\nvidia-smi.exe" }
    if (-not (Test-Path $smi)) { throw "nvidia-smi not found - NVIDIA driver not installed/visible in this machine." }
    $q = & $smi --query-gpu=name,compute_cap,driver_version --format=csv,noheader | Select-Object -First 1
    Say "GPU: $q"
    $parts  = $q.Split(",") | ForEach-Object { $_.Trim() }
    $gpu    = $parts[0]; $cc = $parts[1]; $driver = $parts[2]
    $arch   = "sm_" + ($cc -replace "\.", "")
    $drvMaj = [int]($driver.Split(".")[0])
    Say "Kernel arch: $arch   driver major: $drvMaj"

    New-Item -ItemType Directory -Force -Path $Dl | Out-Null

    # ------------------------------------------------------------------ 2. CUDA
    # CUDA 12.9's nvcc cannot parse the VS 2026 (MSVC 14.5x) standard headers
    # ("tuple index out of bounds" / aligned_storage static_asserts). Use the newest CUDA 13.x.
    $base = "https://developer.download.nvidia.com/compute/cuda/redist"
    if ($env:ZL_CUDA_VERSION) {
        $CudaVer = $env:ZL_CUDA_VERSION
    } else {
        Say "2/5 Looking up the newest CUDA 13.x release"
        $idx = (Invoke-WebRequest "$base/" -UseBasicParsing).Content
        $vers = [regex]::Matches($idx, 'redistrib_(13\.\d+\.\d+)\.json') | ForEach-Object { $_.Groups[1].Value } |
                Sort-Object { [version]$_ } -Unique
        if (-not $vers) { throw "no CUDA 13.x release found at $base" }
        $CudaVer = @($vers)[-1]
    }
    $CudaDir = Join-Path $Root ("cuda_" + $CudaVer)
    $nvcc = Join-Path $CudaDir "bin\nvcc.exe"
    Say "Using CUDA $CudaVer -> $CudaDir"
    if (Test-Path $nvcc) {
        Say "2/5 CUDA already present: $CudaDir"
    } else {
        Say "2/5 Downloading CUDA $CudaVer compiler (redistributable zips)"
        $manifestPath = Join-Path $Dl "redistrib_$CudaVer.json"
        Invoke-WebRequest "$base/redistrib_$CudaVer.json" -OutFile $manifestPath -UseBasicParsing
        $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
        $wanted = @("cuda_nvcc","cuda_crt","libnvvm","cuda_cudart","cuda_cccl","cuda_nvrtc",
                    "cuda_cuobjdump","cuda_nvdisasm","cuda_cuxxfilt","cuda_nvprune","cuda_profiler_api")
        New-Item -ItemType Directory -Force -Path $CudaDir | Out-Null
        foreach ($c in $wanted) {
            $entry = $manifest.$c
            if (-not $entry) { Say "  (component $c not in this release - skipped)"; continue }
            $win = $entry.'windows-x86_64'
            if (-not $win) { Say "  (component $c has no Windows build - skipped)"; continue }
            $zip = Join-Path $Dl ([IO.Path]::GetFileName($win.relative_path))
            if (-not (Test-Path $zip)) {
                Say "  downloading $c ($([math]::Round([double]$win.size/1MB,1)) MB)"
                Invoke-WebRequest "$base/$($win.relative_path)" -OutFile $zip -UseBasicParsing
            }
            $tmp = Join-Path $Dl ("x_" + $c)
            if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
            Copy-Item -Path (Join-Path $inner.FullName "*") -Destination $CudaDir -Recurse -Force
            Remove-Item $tmp -Recurse -Force
        }
        if (-not (Test-Path $nvcc)) { throw "nvcc.exe missing after extraction - see log" }
    }
    Run $nvcc @("--version")

    # ------------------------------------------------------------------ 3. OptiX headers
    $optixTag = if ($drvMaj -ge 570) { "v9.0.0" } elseif ($drvMaj -ge 535) { "v8.0.0" } else { "" }
    $useOptix = $false
    if ($optixTag -eq "") {
        Say "3/5 Driver $driver too old for OptiX 8/9 - building CUDA only (update the NVIDIA driver to enable OptiX)"
    } elseif (Test-Path (Join-Path $OptixDir "include\optix.h")) {
        Say "3/5 OptiX headers already present: $OptixDir"; $useOptix = $true
    } else {
        Say "3/5 Downloading OptiX SDK headers $optixTag (github.com/NVIDIA/optix-dev)"
        try {
            $ozip = Join-Path $Dl "optix-dev-$optixTag.zip"
            Invoke-WebRequest "https://github.com/NVIDIA/optix-dev/archive/refs/tags/$optixTag.zip" -OutFile $ozip -UseBasicParsing
            $otmp = Join-Path $Dl "x_optix"
            if (Test-Path $otmp) { Remove-Item $otmp -Recurse -Force }
            Expand-Archive -Path $ozip -DestinationPath $otmp -Force
            $oinner = Get-ChildItem $otmp -Directory | Select-Object -First 1
            if (Test-Path $OptixDir) { Remove-Item $OptixDir -Recurse -Force }
            Copy-Item -Path $oinner.FullName -Destination $OptixDir -Recurse
            Remove-Item $otmp -Recurse -Force
            $useOptix = Test-Path (Join-Path $OptixDir "include\optix.h")
        } catch { Say "OptiX download failed ($_) - continuing with CUDA only" }
    }

    # ------------------------------------------------------------------ 4. Configure + build
    Say "4/5 Locating Visual Studio x64 build environment"
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    $vsPath = & $vswhere -latest -prerelease -products * -property installationPath | Select-Object -First 1
    $vcvars = Join-Path $vsPath "VC\Auxiliary\Build\vcvars64.bat"
    if (-not (Test-Path $vcvars)) { throw "vcvars64.bat not found under $vsPath" }
    $cmake = (Get-Command cmake -ErrorAction SilentlyContinue).Source
    if (-not $cmake) { $cmake = "C:\Program Files\CMake\bin\cmake.exe" }

    $cudaFwd = $CudaDir -replace "\\", "/"
    $cfg = @(
        "-S", "`"$Src`"", "-B", "`"$Build`"",
        "-UCUDA_*",
        "-DWITH_CYCLES_DEVICE_CUDA=ON",
        "-DWITH_CYCLES_CUDA_BINARIES=ON",
        "-DCYCLES_CUDA_BINARIES_ARCH=$arch",
        "-DCUDA_TOOLKIT_ROOT_DIR=`"$cudaFwd`"",
        "-DCUDA_NVCC_EXECUTABLE=`"$cudaFwd/bin/nvcc.exe`"",
        "-DCUDA_NVCC_FLAGS=`"-allow-unsupported-compiler;-D_ENABLE_EXTENDED_ALIGNED_STORAGE`""
    )
    if ($useOptix) {
        $cfg += @("-DWITH_CYCLES_DEVICE_OPTIX=ON", "-DOPTIX_ROOT_DIR=`"$($OptixDir -replace '\\','/')`"")
    } else {
        $cfg += @("-DWITH_CYCLES_DEVICE_OPTIX=OFF")
    }
    $env:CUDA_PATH = $CudaDir
    $env:PATH = "$CudaDir\bin;$env:PATH"

    $bat = Join-Path $Dl "do_build.cmd"
    @"
@echo off
call "$vcvars" || exit /b 1
"$cmake" $($cfg -join ' ') || exit /b 2
"$cmake" --build "$Build" --config Release --target INSTALL -- /m /nologo /verbosity:minimal || exit /b 3
"@ | Set-Content -Path $bat -Encoding ASCII
    Say "4/5 Configuring + building (GPU kernels for $arch; this can take 20-60 min)"
    $compiledOk = $true
    try {
        RunCmd $bat   # path has no spaces - do not quote (cmd /c quote stripping)
    } catch {
        $compiledOk = $false
        Say "Compiling the GPU kernels locally failed ($_)."
        Say "FALLBACK: build Blender without compiling kernels, then use the official prebuilt Cycles GPU kernels."
    }

    if (-not $compiledOk) {
        # 4b. Build the host side only (CUDA/OptiX device support stays compiled in, kernels not compiled)
        $cfg2 = @("-S", "`"$Src`"", "-B", "`"$Build`"", "-DWITH_CYCLES_CUDA_BINARIES=OFF")
        if ($useOptix) { $cfg2 += @("-DWITH_CYCLES_DEVICE_OPTIX=ON", "-DOPTIX_ROOT_DIR=`"$($OptixDir -replace '\\','/')`"") }
        $bat2 = Join-Path $Dl "do_build_nokernels.cmd"
        @"
@echo off
call "$vcvars" || exit /b 1
"$cmake" $($cfg2 -join ' ') || exit /b 2
"$cmake" --build "$Build" --config Release --target INSTALL -- /m /nologo /verbosity:minimal || exit /b 3
"@ | Set-Content -Path $bat2 -Encoding ASCII
        RunCmd $bat2

        # 4c. Official kernels for the SAME Blender version (kernels must match the source version exactly)
        $blenderExe = Join-Path $Build "bin\Release\blender.exe"
        $old2 = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        $verLine = (& $blenderExe --version 2>&1 | Where-Object { "$_" -match '^Blender \d' } | Select-Object -First 1)
        $ErrorActionPreference = $old2
        if (-not ("$verLine" -match 'Blender (\d+)\.(\d+)\.(\d+)')) { throw "could not read Blender version from: $verLine" }
        $bMaj = $Matches[1]; $bMin = $Matches[2]; $bPat = $Matches[3]
        $bFull = "$bMaj.$bMin.$bPat"; $bShort = "$bMaj.$bMin"
        Say "Self-built Blender version: $bFull"
        $zipName = "blender-$bFull-windows-x64.zip"
        $zipPath = Join-Path $Dl $zipName
        if (-not (Test-Path $zipPath)) {
            $url = "https://download.blender.org/release/Blender$bShort/$zipName"
            Say "Downloading official $zipName (~400 MB) from $url"
            Invoke-WebRequest $url -OutFile $zipPath -UseBasicParsing
        }
        Say "Extracting only the Cycles kernel files"
        Add-Type -AssemblyName System.IO.Compression
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $dest = Join-Path $Build "bin\Release\$bShort\scripts\addons_core\cycles\lib"
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        $zip = [IO.Compression.ZipFile]::OpenRead($zipPath)
        $n = 0
        try {
            foreach ($e in $zip.Entries) {
                if ($e.FullName -match "/$bShort/scripts/addons_core/cycles/lib/[^/]+$") {
                    [IO.Compression.ZipFileExtensions]::ExtractToFile($e, (Join-Path $dest $e.Name), $true)
                    $n++
                }
            }
        } finally { $zip.Dispose() }
        if ($n -eq 0) { throw "no Cycles kernel files found inside $zipName" }
        Say "Copied $n kernel files into $dest"
    }

    # ------------------------------------------------------------------ 5. Verify
    Say "5/5 Checking which GPU backends Blender now sees"
    $blender = Join-Path $Build "bin\Release\blender.exe"
    $py = "import bpy;p=bpy.context.preferences.addons['cycles'].preferences`n" +
          "for b in ('OPTIX','CUDA'):`n" +
          " try:`n  p.compute_device_type=b;p.refresh_devices();print('GPUCHECK',b,[d.name for d in p.devices if d.type==b])`n" +
          " except Exception as e: print('GPUCHECK',b,'ERR',e)"
    $pyFile = Join-Path $Dl "gpucheck.py"; Set-Content -Path $pyFile -Value $py -Encoding ASCII
    $ErrorActionPreference = "Continue"
    & $blender --background --factory-startup --python $pyFile 2>&1 | Where-Object { "$_" -match "GPUCHECK" } |
        ForEach-Object { Say "$_" }

    Say "=== DONE. Run test_blender_render.bat - log should say 'backend OPTIX: OK' or 'backend CUDA: OK'. ==="
}
catch {
    Say "!!! FAILED: $_"
    Say "Send gpu_setup.log to Claude."
}
Read-Host "Press Enter to close"
