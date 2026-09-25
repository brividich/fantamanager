<#
    build_windows.ps1 — one-shot Windows build for FantaManager.

    Produces dist\FantaManager-Setup.exe (a per-user auto-installer).

    Usage (from the project root, in PowerShell):
        .\packaging\build_windows.ps1

    Steps: ensure deps -> PyInstaller bundle -> Inno Setup installer.
#>
$ErrorActionPreference = "Stop"

# Run from the project root regardless of where the script was invoked.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Prefer the project venv; fall back to whatever 'python' is on PATH.
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "==> Installing build dependencies..." -ForegroundColor Cyan
& $py -m pip install --quiet -r requirements.txt
& $py -m pip install --quiet "pyinstaller>=6.0,<7.0"

Write-Host "==> Building the app bundle (PyInstaller)..." -ForegroundColor Cyan
& $py -m PyInstaller --clean --noconfirm packaging\fantamanager.spec

# Locate the Inno Setup compiler.
$iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    Write-Warning "Inno Setup 6 non trovato. Scaricalo da https://jrsoftware.org/isdl.php"
    Write-Warning "Il bundle è comunque pronto in dist\FantaManager\ (cartella eseguibile)."
    exit 1
}

Write-Host "==> Building the installer (Inno Setup)..." -ForegroundColor Cyan
& $iscc packaging\installer.iss

Write-Host ""
Write-Host "OK -> dist\FantaManager-Setup.exe" -ForegroundColor Green
