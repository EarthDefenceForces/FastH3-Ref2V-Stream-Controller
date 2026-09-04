$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PortableRoot = Split-Path -Parent $ScriptDir
$PythonExe = Join-Path $PortableRoot "python_embeded\python.exe"
$StreamScript = Join-Path $ScriptDir "stream_h3_r2v_continuity.py"

if (-not (Test-Path $PythonExe)) {
    throw "ComfyUI Portable Python wurde nicht gefunden: $PythonExe"
}

if (-not (Test-Path (Join-Path $ScriptDir "submit_h3.py"))) {
    throw "submit_h3.py fehlt im selben Ordner wie stream_h3_r2v_continuity.py"
}

function Test-NodeTreeChanged {
    param([string]$SourceDir, [string]$TargetDir)
    if (-not (Test-Path $TargetDir)) { return $true }
    foreach ($SourceFile in Get-ChildItem -Path $SourceDir -File -Recurse) {
        $Relative = $SourceFile.FullName.Substring($SourceDir.Length).TrimStart([char[]]"\/")
        $TargetFile = Join-Path $TargetDir $Relative
        if (-not (Test-Path $TargetFile)) { return $true }
        if ((Get-FileHash $SourceFile.FullName).Hash -ne (Get-FileHash $TargetFile).Hash) {
            return $true
        }
    }
    return $false
}

$NodeNames = @("h3_r2v_fixed", "h3_fast_writer", "h3_block_attention")
$InstalledNodes = @()
foreach ($NodeName in $NodeNames) {
    $SourceDir = Join-Path $ScriptDir "custom_nodes\$NodeName"
    if (-not (Test-Path $SourceDir)) { continue }
    $TargetDir = Join-Path $PortableRoot "ComfyUI\custom_nodes\$NodeName"
    if (Test-NodeTreeChanged $SourceDir $TargetDir) {
        New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
        Copy-Item -Path (Join-Path $SourceDir "*") -Destination $TargetDir -Recurse -Force
        $InstalledNodes += $NodeName
    }
}

if ($InstalledNodes.Count -gt 0) {
    Write-Host "Custom Nodes wurden installiert/aktualisiert:" -ForegroundColor Green
    foreach ($NodeName in $InstalledNodes) {
        Write-Host "  $NodeName"
    }
    Write-Host "Bitte ComfyUI jetzt vollständig neu starten und danach dieses Script erneut ausführen." -ForegroundColor Yellow
    exit 0
}

& $PythonExe $StreamScript @args
exit $LASTEXITCODE
