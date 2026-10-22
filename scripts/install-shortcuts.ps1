param([switch]$Startup, [switch]$Remove, [switch]$WhatIf)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exe = @((Join-Path $root 'CodexResetCardDesktop.exe'), (Join-Path $root 'dist\CodexResetCardDesktop.exe')) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
$vbs = Join-Path $root 'desktop\启动.vbs'
if (-not $exe -and -not (Test-Path -LiteralPath $vbs)) { throw 'Application files are missing.' }
$entries = @(@{Folder=[Environment]::GetFolderPath('DesktopDirectory');Name='Codex Reset Card.lnk';Minimized=$false})
if ($Startup) { $entries += @{Folder=[Environment]::GetFolderPath('Startup');Name='Codex Reset Card Panel.lnk';Minimized=$true} }
$shell = New-Object -ComObject WScript.Shell
foreach ($entry in $entries) {
    $path = Join-Path $entry.Folder $entry.Name
    $target = if ($exe) { $exe } else { "$env:SystemRoot\System32\wscript.exe" }
    $arguments = if ($exe) { '' } else { '"' + $vbs + '"' }
    if ($entry.Minimized) { $arguments += ' --minimized' }
    if ($WhatIf) { @{path=$path;target=$target;arguments=$arguments;remove=[bool]$Remove} | ConvertTo-Json; continue }
    if (Test-Path -LiteralPath $path) {
        $old = $shell.CreateShortcut($path)
        if ($old.TargetPath -ne $target -or $old.Arguments.Trim() -ne $arguments.Trim()) { throw ('Unrelated shortcut exists: ' + $path) }
    }
    if ($Remove) { if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }; continue }
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $target
    $link.Arguments = $arguments.Trim()
    $link.WorkingDirectory = $root
    $link.IconLocation = "$env:SystemRoot\System32\shell32.dll,44"
    $link.Description = 'Codex Reset Card Desktop'
    $link.Save()
    Write-Output $path
}
