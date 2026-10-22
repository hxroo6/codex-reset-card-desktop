param([switch]$Demo, [switch]$Minimized, [switch]$ShowConsole, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exe = @((Join-Path $root 'CodexResetCardDesktop.exe'), (Join-Path $root 'dist\CodexResetCardDesktop.exe')) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
$panelArgs = @()
if ($Demo) { $panelArgs += '--demo' }
if ($Minimized) { $panelArgs += '--minimized' }
if ($exe) {
    $target = $exe
    $arguments = $panelArgs
} else {
    $pythonPath = $null
    foreach ($candidate in @(@{Name='py';Args=@('-3.12')}, @{Name='py';Args=@('-3')}, @{Name='python';Args=@()})) {
        $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $probeArgs = $candidate.Args + @('-c', 'import sys,tkinter; assert sys.version_info >= (3,12); print(sys.executable)')
        $probe = & $command.Source @probeArgs 2>$null
        if ($LASTEXITCODE -eq 0 -and $probe) { $pythonPath = [string]@($probe)[-1]; break }
    }
    if (-not $pythonPath) { throw 'Download the standalone EXE, or install Python 3.12+ with tkinter.' }
    $windowed = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
    $target = if (-not $ShowConsole -and (Test-Path -LiteralPath $windowed)) { $windowed } else { $pythonPath }
    $arguments = @(('"' + (Join-Path $PSScriptRoot 'reset_card_panel.py') + '"')) + $panelArgs
}
if ($CheckOnly) { @{target=$target;arguments=@($arguments);requiresPython=(-not [bool]$exe)} | ConvertTo-Json; exit 0 }
if ($ShowConsole -and -not $exe) { & $target (Join-Path $PSScriptRoot 'reset_card_panel.py') @panelArgs; exit $LASTEXITCODE }
if ($arguments.Count) { Start-Process -FilePath $target -ArgumentList $arguments -WindowStyle Hidden | Out-Null }
else { Start-Process -FilePath $target -WindowStyle Hidden | Out-Null }
