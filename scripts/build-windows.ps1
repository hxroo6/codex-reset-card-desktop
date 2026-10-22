param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    & $Python -m PyInstaller --noconfirm --onefile --windowed --noupx --name CodexResetCardDesktop --paths (Join-Path $root 'desktop') --distpath dist --workpath build --specpath build --add-data ((Join-Path $root 'LICENSE') + ':.') --add-data ((Join-Path $root 'LICENSES') + ':LICENSES') 'desktop\reset_card_panel.py'
    if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
    Write-Output (Join-Path $root 'dist\CodexResetCardDesktop.exe')
} finally { Pop-Location }
