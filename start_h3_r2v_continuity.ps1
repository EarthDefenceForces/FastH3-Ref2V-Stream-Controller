$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StreamScript = Join-Path $ScriptDir "stream_h3_r2v_continuity.py"

function New-ComfyEnvironment {
    param(
        [string]$Kind,
        [string]$Root,
        [string]$DataRoot,
        [string]$Python
    )
    [PSCustomObject]@{
        Kind = $Kind
        Root = $Root
        DataRoot = $DataRoot
        Python = $Python
    }
}

function Test-ComfyEnvironment {
    param($Environment)
    if (-not $Environment) { return $false }
    if (-not (Test-Path -LiteralPath $Environment.Root -PathType Container)) { return $false }
    if (-not (Test-Path -LiteralPath $Environment.Python -PathType Leaf)) { return $false }
    return $true
}

function Find-ComfyInstallation {
    # Explicit environment variables win. They are useful for custom locations
    # and for choosing one installation when Comfy Desktop manages several.
    if ($env:COMFYUI_ROOT) {
        $ExplicitRoot = [Environment]::ExpandEnvironmentVariables($env:COMFYUI_ROOT)
        $ExplicitData = if ($env:COMFYUI_DATA_ROOT) {
            [Environment]::ExpandEnvironmentVariables($env:COMFYUI_DATA_ROOT)
        } else {
            $ExplicitRoot
        }
        $ExplicitPython = if ($env:COMFYUI_PYTHON) {
            [Environment]::ExpandEnvironmentVariables($env:COMFYUI_PYTHON)
        } elseif (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $ExplicitRoot) ".venv\Scripts\python.exe")) {
            Join-Path (Split-Path -Parent $ExplicitRoot) ".venv\Scripts\python.exe"
        } elseif (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $ExplicitRoot) "python_embeded\python.exe")) {
            Join-Path (Split-Path -Parent $ExplicitRoot) "python_embeded\python.exe"
        } else {
            Join-Path $ExplicitRoot ".venv\Scripts\python.exe"
        }
        return (New-ComfyEnvironment "custom" $ExplicitRoot $ExplicitData $ExplicitPython)
    }

    # ComfyUI Portable: this add-on normally sits in fasth3-live directly next
    # to ComfyUI and python_embeded.
    $PortableBase = Split-Path -Parent $ScriptDir
    $Portable = New-ComfyEnvironment `
        "portable" `
        (Join-Path $PortableBase "ComfyUI") `
        (Join-Path $PortableBase "ComfyUI") `
        (Join-Path $PortableBase "python_embeded\python.exe")
    if (Test-ComfyEnvironment $Portable) { return $Portable }

    # Current Comfy Desktop defaults. If several installations exist, prefer
    # the most recently modified valid one.
    $DesktopLayouts = @()
    if ($env:LOCALAPPDATA) {
        $DesktopLayouts += [PSCustomObject]@{
            Installs = Join-Path $env:LOCALAPPDATA "Comfy-Desktop\ComfyUI-Installs"
            Shared = Join-Path $env:LOCALAPPDATA "Comfy-Desktop\ComfyUI-Shared"
            Kind = "desktop"
        }
    }
    if ($env:USERPROFILE) {
        # Legacy Comfy Desktop used these locations before the LocalAppData
        # layout. Keeping them here costs nothing and avoids a manual override.
        $DesktopLayouts += [PSCustomObject]@{
            Installs = Join-Path $env:USERPROFILE "ComfyUI-Installs"
            Shared = Join-Path $env:USERPROFILE "ComfyUI-Shared"
            Kind = "desktop-legacy"
        }
    }

    foreach ($Layout in $DesktopLayouts) {
        if (-not (Test-Path -LiteralPath $Layout.Installs -PathType Container)) { continue }
        $InstallDirs = Get-ChildItem -LiteralPath $Layout.Installs -Directory -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
        foreach ($InstallDir in $InstallDirs) {
            $RootCandidates = @(
                (Join-Path $InstallDir.FullName "ComfyUI"),
                $InstallDir.FullName
            )
            $PythonCandidates = @(
                (Join-Path $InstallDir.FullName ".venv\Scripts\python.exe"),
                (Join-Path $InstallDir.FullName "venv\Scripts\python.exe"),
                (Join-Path $InstallDir.FullName "python_embeded\python.exe")
            )
            foreach ($Root in $RootCandidates) {
                foreach ($Python in $PythonCandidates) {
                    $Candidate = New-ComfyEnvironment $Layout.Kind $Root $Layout.Shared $Python
                    if (Test-ComfyEnvironment $Candidate) { return $Candidate }
                }
            }
        }
    }

    # Legacy single-install Desktop builds commonly keep user data and their
    # venv in Documents\ComfyUI while the bundled ComfyUI source lives below
    # LocalAppData\Programs.
    if ($env:USERPROFILE -and $env:LOCALAPPDATA) {
        $DocumentsData = Join-Path $env:USERPROFILE "Documents\ComfyUI"
        $LegacyPython = Join-Path $DocumentsData ".venv\Scripts\python.exe"
        $LegacyRoots = @(
            (Join-Path $env:LOCALAPPDATA "Programs\ComfyUI\resources\ComfyUI"),
            (Join-Path $env:LOCALAPPDATA "Programs\@comfyorgcomfyui-electron\resources\ComfyUI")
        )
        foreach ($LegacyRoot in $LegacyRoots) {
            $Candidate = New-ComfyEnvironment "desktop-legacy-single" $LegacyRoot $DocumentsData $LegacyPython
            if (Test-ComfyEnvironment $Candidate) { return $Candidate }
        }
    }

    return $null
}

$Comfy = Find-ComfyInstallation
if (-not $Comfy) {
    throw @"
Keine kompatible ComfyUI-Installation wurde gefunden.

Automatisch geprüft wurden:
  - ComfyUI Portable direkt neben diesem fasth3-live-Ordner
  - %LOCALAPPDATA%\Comfy-Desktop\ComfyUI-Installs
  - %USERPROFILE%\ComfyUI-Installs (ältere Desktop-Versionen)

Für einen eigenen Installationsort setze vor dem Start:
  `$env:COMFYUI_ROOT = "`$env:LOCALAPPDATA\Comfy-Desktop\ComfyUI-Installs\<Name>\ComfyUI"
  `$env:COMFYUI_DATA_ROOT = "`$env:LOCALAPPDATA\Comfy-Desktop\ComfyUI-Shared"
  `$env:COMFYUI_PYTHON = "`$env:LOCALAPPDATA\Comfy-Desktop\ComfyUI-Installs\<Name>\.venv\Scripts\python.exe"
"@
}

$PythonExe = $Comfy.Python
$ComfyInput = Join-Path $Comfy.DataRoot "input"
$ComfyOutput = Join-Path $Comfy.DataRoot "output"

Write-Host "ComfyUI erkannt ($($Comfy.Kind)):" -ForegroundColor Cyan
Write-Host "  Core:   $($Comfy.Root)"
Write-Host "  Daten:  $($Comfy.DataRoot)"
Write-Host "  Python: $PythonExe"

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
    $TargetDir = Join-Path $Comfy.Root "custom_nodes\$NodeName"
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

& $PythonExe $StreamScript --comfy-input $ComfyInput --comfy-output $ComfyOutput @args
exit $LASTEXITCODE
