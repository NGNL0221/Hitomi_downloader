$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "hitomi多线程下载器" `
    --distpath ".\dist" `
    --workpath ".\build" `
    ".\downloader.py"

Write-Host "Build complete: $projectDir\dist\hitomi多线程下载器.exe"
