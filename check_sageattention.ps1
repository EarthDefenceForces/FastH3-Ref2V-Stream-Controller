$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PortableRoot = Split-Path -Parent $ScriptDir
$PythonExe = Join-Path $PortableRoot "python_embeded\python.exe"

if (-not (Test-Path $PythonExe)) {
    throw "ComfyUI Portable Python wurde nicht gefunden: $PythonExe"
}

& $PythonExe -c "import sys, torch; print('Python:', sys.version); print('Torch:', torch.__version__); print('Torch CUDA:', torch.version.cuda); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'nicht verfügbar')"
& $PythonExe -c "import sageattention; print('SageAttention importiert:', getattr(sageattention, '__version__', 'Version unbekannt'))"

if ($LASTEXITCODE -ne 0) {
    Write-Host "`nSageAttention ist in diesem Portable-Python nicht verwendbar." -ForegroundColor Yellow
    Write-Host "Installiere nur ein Wheel, das exakt zu Torch, CUDA und Windows ABI passt."
    exit 1
}

Write-Host "`nImport erfolgreich. Starte ComfyUI neu und prüfe in der Control UI, ob 'sage attention' angeboten wird." -ForegroundColor Green
