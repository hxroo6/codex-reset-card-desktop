param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root 'dist\CodexResetCardDesktop.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Build the standalone EXE first.' }
$version = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
$name = 'CodexResetCardDesktop-v' + $version + '-windows-x64'
$stage = Join-Path (Join-Path $root 'dist') ('package-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage,(Join-Path $stage 'scripts'),(Join-Path $stage 'LICENSES') | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage 'docs\images') | Out-Null
foreach ($image in @('dashboard.png','floating-countdown.png','result-notification.png')) {
    Copy-Item -LiteralPath (Join-Path $root ('docs\images\' + $image)) -Destination (Join-Path $stage 'docs\images')
}
Copy-Item -LiteralPath $exe -Destination $stage
foreach ($file in @('README.md','LICENSE','NOTICE')) { Copy-Item -LiteralPath (Join-Path $root $file) -Destination $stage }
Copy-Item -LiteralPath (Join-Path $root 'scripts\install-shortcuts.ps1') -Destination (Join-Path $stage 'scripts')
Get-ChildItem -LiteralPath (Join-Path $root 'LICENSES') -File | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $stage 'LICENSES') }
$runtimeJson = & $Python -c "import sys,json,tkinter;from pathlib import Path;from importlib.metadata import distribution;d=distribution('pyinstaller');print(json.dumps({'python':str(Path(sys.base_prefix)/'LICENSE.txt'),'tk':str(Path(sys.base_prefix)/'tcl'/'tk8.6'/'license.terms'),'tcl':str(tkinter.Tcl().call('info','patchlevel')),'pyinstaller':next(str(d.locate_file(p)) for p in d.files if str(p).endswith('licenses/COPYING.txt'))}))"
if ($LASTEXITCODE -ne 0) { throw 'Cannot find runtime license notices.' }
$runtime = $runtimeJson | ConvertFrom-Json
Copy-Item -LiteralPath $runtime.python -Destination (Join-Path $stage 'LICENSES\Python-LICENSE.txt')
Copy-Item -LiteralPath $runtime.tk -Destination (Join-Path $stage 'LICENSES\Tk-LICENSE.txt')
Copy-Item -LiteralPath $runtime.pyinstaller -Destination (Join-Path $stage 'LICENSES\PyInstaller-COPYING.txt')
$tclTag = 'core-' + $runtime.tcl.Replace('.','-')
Invoke-WebRequest -UseBasicParsing -Uri ('https://raw.githubusercontent.com/tcltk/tcl/' + $tclTag + '/license.terms') -OutFile (Join-Path $stage 'LICENSES\Tcl-LICENSE.txt') -TimeoutSec 30
$startDemo = "@echo off`r`nstart `"`" `"%~dp0CodexResetCardDesktop.exe`" --demo`r`n"
[IO.File]::WriteAllText((Join-Path $stage 'Demo.cmd'),$startDemo,[Text.UTF8Encoding]::new($false))
$zip = Join-Path (Join-Path $root 'dist') ($name + '.zip')
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force
$hash = Get-FileHash -LiteralPath $zip -Algorithm SHA256
[IO.File]::WriteAllText(($zip + '.sha256'),($hash.Hash.ToLowerInvariant() + '  ' + [IO.Path]::GetFileName($zip) + "`n"),[Text.UTF8Encoding]::new($false))
Write-Output $zip
Write-Output ($zip + '.sha256')
