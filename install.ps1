<#
.SYNOPSIS
  One-command installer for Codex Continuity Guard (Windows, Codex Desktop).

.DESCRIPTION
  1. Finds Python 3.11 (offers to install it with winget if missing).
  2. Creates a private virtualenv and installs the single pinned dependency.
  3. Opens providers.json so you can add YOUR providers and models, then asks for
     each provider's API key (stored encrypted for your Windows account only).
  4. Starts the local gateway, points Codex Desktop at it and registers it to start at logon.

  Run it again any time to apply changes to providers.json or to update.

.PARAMETER Root        Installation folder. Default: %LOCALAPPDATA%\CodexContinuityGuard
.PARAMETER CodexHome   Codex configuration folder. Default: %USERPROFILE%\.codex
.PARAMETER NoStartup   Do not register the start-at-logon entry.
.PARAMETER Yes         Never prompt (requires an existing providers.json and stored keys).
#>
[CmdletBinding()]
param(
  [string]$Root = (Join-Path $env:LOCALAPPDATA 'CodexContinuityGuard'),
  [string]$CodexHome = (Join-Path $env:USERPROFILE '.codex'),
  [switch]$NoStartup,
  [switch]$Yes
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
# The one runtime dependency, pinned by version and hash (the request body codec).
$requirement = 'zstandard==0.25.0 --hash=sha256:daab68faadb847063d0c56f361a289c4f268706b598afbf9ad113cbe5c38b6b2'

function Find-Python311 {
  $found = @()
  if (Get-Command py -ErrorAction SilentlyContinue) {
    $found += (& py -3.11 -c "import sys;print(sys.executable)" 2>$null)
  }
  $found += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'), 'C:\Program Files\Python311\python.exe'
  foreach ($candidate in $found) {
    # The Microsoft Store Python is an app alias the service cannot pin; require a regular install.
    if ($candidate -and (Test-Path $candidate) -and ($candidate -notmatch 'WindowsApps')) { return $candidate }
  }
  return $null
}

Write-Host "`nCodex Continuity Guard installer" -ForegroundColor Cyan
if ($env:OS -ne 'Windows_NT') { throw 'This installer supports Windows only.' }

$python = Find-Python311
if (-not $python) {
  Write-Host 'Python 3.11 (64-bit, from python.org) is required and was not found.' -ForegroundColor Yellow
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'Install Python 3.11 from https://www.python.org/downloads/release/python-3119/ and run this script again.'
  }
  if (-not $Yes) {
    $answer = Read-Host 'Install Python 3.11 now with winget (for your user only)? [Y/n]'
    if ($answer -and $answer -notmatch '^[Yy]') { throw 'Python 3.11 is required. Nothing was changed.' }
  }
  winget install --exact --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
  $python = Find-Python311
  if (-not $python) { throw 'Python 3.11 was installed but could not be located. Open a new terminal and run this script again.' }
}
Write-Host "Python:  $python"

New-Item -ItemType Directory -Force $Root | Out-Null
$venvPython = Join-Path $Root 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
  Write-Host "Creating private virtualenv in $Root\venv ..."
  & $python -m venv (Join-Path $Root 'venv')
  if ($LASTEXITCODE) { throw 'Could not create the virtualenv.' }
}
$requirements = Join-Path $Root 'requirements.lock'
Set-Content -Path $requirements -Value $requirement -Encoding ascii
Write-Host 'Installing the pinned dependency (zstandard) ...'
& $venvPython -m pip install --quiet --disable-pip-version-check --only-binary=:all: --require-hashes -r $requirements
if ($LASTEXITCODE) { throw 'Could not install the pinned dependency. Check your internet connection and run this script again.' }

$arguments = @('-B', (Join-Path $repo 'guardctl.py'), '--root', $Root, '--codex-home', $CodexHome, 'install')
if ($NoStartup) { $arguments += '--no-startup' }
if ($Yes) { $arguments += '--yes' }
& $venvPython @arguments
exit $LASTEXITCODE
