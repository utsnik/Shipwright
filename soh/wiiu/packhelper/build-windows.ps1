$ErrorActionPreference = "Stop"

$HelperDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $HelperDir

python -c "import tkinter, numpy, PIL, PyInstaller"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Install Python with tkinter, then run: python -m pip install pyinstaller numpy Pillow"
}

python -m PyInstaller --clean --noconfirm `
    --distpath (Join-Path $HelperDir "dist") `
    --workpath (Join-Path $HelperDir "build") `
    (Join-Path $HelperDir "SoH-WiiU-Pack-Helper.spec")

Write-Host "Built $HelperDir\dist\SoH-WiiU-Pack-Helper.exe"
