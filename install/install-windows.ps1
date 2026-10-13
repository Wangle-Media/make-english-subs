# MakeSubs installer for Windows (NVIDIA GPU recommended).
# Run in PowerShell:
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Wangle-Media/make-english-subs/main/install/install-windows.ps1 | iex"
# Re-running it updates MakeSubs.

$ErrorActionPreference = "Stop"
$Ref = if ($env:MAKESUBS_REF) { $env:MAKESUBS_REF } else { "main" }
$Source = "https://github.com/Wangle-Media/make-english-subs/archive/refs/heads/$Ref.zip"
$Bin = Join-Path $env:USERPROFILE ".local\bin"
$LlmModel = "gemma3:12b"

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

Step "Checking graphics card"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
} else {
    Write-Host "No NVIDIA GPU found. MakeSubs will work, but slowly (CPU only)." -ForegroundColor Yellow
}

Step "Installing uv (manages Python for MakeSubs; nothing else on the system is changed)"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$Bin;$env:Path"
}
uv --version

Step "Installing MakeSubs ($Ref)"
uv tool install --force --python 3.12 "makesubs @ $Source"
uv tool update-shell | Out-Null

Step "Optional: high-quality translation (Ollama + about 8 GB model)"
Write-Host "Gives much better English, and enables Danish and other languages."
$answer = Read-Host "Install it? [Y/n]"
if ($answer -notmatch '^[nN]') {
    $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (-not (Test-Path $ollama)) {
        $setup = Join-Path $env:TEMP "OllamaSetup.exe"
        Write-Host "Downloading Ollama..."
        Invoke-WebRequest "https://ollama.com/download/OllamaSetup.exe" -OutFile $setup -UseBasicParsing
        Start-Process $setup -ArgumentList "/SILENT" -Wait
    }
    # The Ollama app starts its server automatically; give it a moment.
    for ($i = 0; $i -lt 30; $i++) {
        try { Invoke-WebRequest "http://127.0.0.1:11434/api/tags" -UseBasicParsing | Out-Null; break }
        catch { if ($i -eq 0) { Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden }; Start-Sleep 2 }
    }
    & $ollama pull $LlmModel
}

Step "Downloading the speech model (about 3 GB, once)"
& (Join-Path $Bin "makesubs.exe") --setup

Step "Creating desktop shortcut"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "MakeSubs.lnk"))
$lnk.TargetPath = Join-Path $Bin "makesubs-gui.exe"
$lnk.Save()

Write-Host "`nDone. Open 'MakeSubs' on your desktop." -ForegroundColor Green
