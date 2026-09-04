$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-ComfyPython {
    if ($env:COMFYUI_PYTHON) {
        $Explicit = [Environment]::ExpandEnvironmentVariables($env:COMFYUI_PYTHON)
        if (Test-Path -LiteralPath $Explicit -PathType Leaf) { return $Explicit }
    }

    $Portable = Join-Path (Split-Path -Parent $ScriptDir) "python_embeded\python.exe"
    if (Test-Path -LiteralPath $Portable -PathType Leaf) { return $Portable }

    $InstallRoots = @()
    if ($env:LOCALAPPDATA) {
        $InstallRoots += Join-Path $env:LOCALAPPDATA "Comfy-Desktop\ComfyUI-Installs"
    }
    if ($env:USERPROFILE) {
        $InstallRoots += Join-Path $env:USERPROFILE "ComfyUI-Installs"
    }
    foreach ($InstallRoot in $InstallRoots) {
        if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) { continue }
        $InstallDirs = Get-ChildItem -LiteralPath $InstallRoot -Directory -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
        foreach ($InstallDir in $InstallDirs) {
            foreach ($Relative in @(
                ".venv\Scripts\python.exe",
                "venv\Scripts\python.exe",
                "python_embeded\python.exe"
            )) {
                $Candidate = Join-Path $InstallDir.FullName $Relative
                if (Test-Path -LiteralPath $Candidate -PathType Leaf) { return $Candidate }
            }
        }
    }

    if ($env:USERPROFILE) {
        $Legacy = Join-Path $env:USERPROFILE "Documents\ComfyUI\.venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $Legacy -PathType Leaf) { return $Legacy }
    }
    return $null
}

$PythonExe = Find-ComfyPython
if (-not $PythonExe) {
    throw @"
ComfyUI-Python wurde nicht gefunden. Setze für einen eigenen Installationsort:
  `$env:COMFYUI_PYTHON = "`$env:LOCALAPPDATA\Comfy-Desktop\ComfyUI-Installs\<Name>\.venv\Scripts\python.exe"
"@
}

Write-Host "Python: $PythonExe" -ForegroundColor Cyan
& $PythonExe -c "import sys, torch; print('Python:', sys.version); print('Torch:', torch.__version__); print('Torch CUDA:', torch.version.cuda); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'nicht verfügbar')"
& $PythonExe -c "import sageattention; print('SageAttention importiert:', getattr(sageattention, '__version__', 'Version unbekannt'))"

if ($LASTEXITCODE -ne 0) {
    Write-Host "`nSageAttention ist in diesem ComfyUI-Python nicht verwendbar." -ForegroundColor Yellow
    Write-Host "Installiere nur ein Wheel, das exakt zu Torch, CUDA und Windows ABI passt."
    exit 1
}

Write-Host "`nImport erfolgreich. Starte ComfyUI neu und prüfe in der Control UI, ob 'sage attention' angeboten wird." -ForegroundColor Green
