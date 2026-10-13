# MakeSubs installer for Windows (NVIDIA GPU recommended).
# Run in PowerShell:
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Wangle-Media/make-english-subs/main/install/install-windows.ps1 | iex"
# Re-running it updates MakeSubs.

$ErrorActionPreference = "Stop"
$Ref = if ($env:MAKESUBS_REF) { $env:MAKESUBS_REF } else { "main" }
$Source = "https://github.com/Wangle-Media/make-english-subs/archive/refs/heads/$Ref.zip"
$Bin = Join-Path $env:USERPROFILE ".local\bin"
$LlmModel = "gemma3:12b"
$MinDriver = [version]"527.41"  # oldest Windows driver for CUDA 12

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host $msg -ForegroundColor Yellow }
# PowerShell doesn't stop when a program (uv, ollama, makesubs) fails, so check each one.
function Check($what) {
    if ($LASTEXITCODE -ne 0) {
        throw "$what failed (exit code $LASTEXITCODE), so the installation stopped. " +
              "Copy the text in this window and send it to whoever set up MakeSubs for you."
    }
}

Step "Checking graphics card"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpu = (nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | Select-Object -First 1)
    Write-Host $gpu
    $driver = $gpu.Split(",")[-1].Trim()
    $parsed = $null
    if ([version]::TryParse($driver, [ref]$parsed) -and $parsed -lt $MinDriver) {
        Warn "The NVIDIA driver ($driver) is too old for MakeSubs; it needs $MinDriver or newer."
        Warn "Update it from https://www.nvidia.com/Download/index.aspx and run this installer again."
        Warn "(Without the update MakeSubs still works, but on the CPU, which is very slow.)"
    }
} else {
    Warn "No NVIDIA GPU found. MakeSubs will work, but slowly (CPU only)."
}

Step "Installing uv (manages Python for MakeSubs; nothing else on the system is changed)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    Check "Installing uv"
    $env:Path = "$Bin;$env:Path"
}
uv --version
Check "uv"

Step "Installing MakeSubs ($Ref)"
# --reinstall-package: always fetch the latest code, even though the download link is the same.
uv tool install --force --reinstall-package makesubs --python 3.12 "makesubs @ $Source"
Check "Installing MakeSubs"
uv tool update-shell | Out-Null

Step "Optional: high-quality translation (Ollama + about 8 GB model)"
Write-Host "Gives much better English, and enables Danish and other languages."
$answer = Read-Host "Install it? [Y/n]"
if ($answer -notmatch '^[nN]') {
    $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
    if (-not (Test-Path $ollama)) {
        $setup = Join-Path $env:TEMP "OllamaSetup.exe"
        Write-Host "Downloading Ollama..."
        Invoke-WebRequest "https://ollama.com/download/OllamaSetup.exe" -OutFile $setup -UseBasicParsing
        Start-Process $setup -ArgumentList "/SILENT" -Wait
        Remove-Item $setup -ErrorAction SilentlyContinue
    }
    # The Ollama app normally starts its server itself; start it if not.
    $up = $false
    for ($i = 0; $i -lt 30 -and -not $up; $i++) {
        try { Invoke-WebRequest "http://127.0.0.1:11434/api/tags" -UseBasicParsing | Out-Null; $up = $true }
        catch { if ($i -eq 0) { Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden }; Start-Sleep 2 }
    }
    if ($up) {
        & $ollama pull $LlmModel
        Check "Downloading the translation model"
    } else {
        Warn "Ollama didn't start. Restart the PC, open Ollama from the Start menu, and run this installer again."
    }
}

Step "Downloading and testing the speech model (about 3 GB, once)"
& (Join-Path $Bin "makesubs.exe") --setup
Check "Speech model setup"

Step "Creating desktop shortcut"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "MakeSubs.lnk"))
$lnk.TargetPath = Join-Path $Bin "makesubs-gui.exe"
$lnk.WorkingDirectory = $env:USERPROFILE
$lnk.Save()

Write-Host "`nDone. Open 'MakeSubs' on your desktop." -ForegroundColor Green
