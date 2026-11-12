<#
.SYNOPSIS
  Uninstall Codex Continuity Guard: stop the service, restore Codex Desktop's routing, remove the logon entry.

.PARAMETER Root       Installation folder. Default: %LOCALAPPDATA%\CodexContinuityGuard
.PARAMETER CodexHome  Codex configuration folder. Default: %USERPROFILE%\.codex
.PARAMETER Purge      Also delete the installation folder (chat state, receipts and encrypted keys).
#>
[CmdletBinding()]
param(
  [string]$Root = (Join-Path $env:LOCALAPPDATA 'CodexContinuityGuard'),
  [string]$CodexHome = (Join-Path $env:USERPROFILE '.codex'),
  [switch]$Purge
)
$ErrorActionPreference = 'Stop'
$venvPython = Join-Path $Root 'venv\Scripts\python.exe'
$guardctl = Join-Path $Root 'guardctl.py'
if ((Test-Path $venvPython) -and (Test-Path (Join-Path $Root 'current.json'))) {
  & $venvPython -B $guardctl --root $Root --codex-home $CodexHome uninstall
  if ($LASTEXITCODE) { throw 'Uninstall did not complete; nothing was deleted.' }
} else {
  Write-Host "No active installation found in $Root."
}
if ($Purge -and (Test-Path $Root)) {
  Remove-Item -Recurse -Force $Root
  Write-Host "Deleted $Root."
}
