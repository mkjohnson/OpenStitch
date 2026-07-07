$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller

& ".\.venv\Scripts\python.exe" -m PyInstaller `
    --clean `
    --noconfirm `
    --onefile `
    --name "EmbroideryUtility" `
    --add-data "templates;templates" `
    --add-data "static;static" `
    app_launcher.py

Write-Host ""
Write-Host "Built: $repo\dist\EmbroideryUtility.exe"
Write-Host "Share that EXE with the person using the app. It will create viewer_output beside itself."
