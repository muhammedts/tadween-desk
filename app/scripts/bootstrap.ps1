<#
.SYNOPSIS
  Create or update the application's development virtual environment from the hash-locked requirements.

.DESCRIPTION
  Works from any current directory: it switches to the app/ folder (this script's parent's parent).
  Installs app/requirements-dev.lock.txt with --require-hashes and binary wheels only, then
  installs the application in editable mode without resolving further dependencies.
  Never deletes an existing environment. Downloads packages from PyPI only; it does not
  download models, open audio devices or enable any cloud service.

.EXAMPLE
  # From the project root (creates app\.venv):
  powershell -ExecutionPolicy Bypass -File app\scripts\bootstrap.ps1
  # Separate clean environment:
  powershell -ExecutionPolicy Bypass -File app\scripts\bootstrap.ps1 -VenvPath C:\temp\mm-clean
  # Without the py launcher, name the CPython 3.11 interpreter explicitly:
  powershell -ExecutionPolicy Bypass -File app\scripts\bootstrap.ps1 -Python C:\path\to\python.exe
#>
[CmdletBinding()]
param(
    [string]$VenvPath = '.venv',
    [string]$PythonVersion = '3.11',
    # Optional explicit interpreter used instead of the `py -<PythonVersion>` launcher.
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$app = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $app

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

if (-not (Test-Path -LiteralPath (Join-Path $VenvPath 'Scripts\python.exe'))) {
    if ($Python) {
        # The lock files are resolved for this exact minor version.
        Invoke-Checked $Python @('-c', "import sys; sys.exit(0 if '%d.%d' % sys.version_info[:2] == '$PythonVersion' else 1)")
        Invoke-Checked $Python @('-m', 'venv', $VenvPath)
    } else {
        Invoke-Checked 'py' @("-$PythonVersion", '-m', 'venv', $VenvPath)
    }
}
$python = Join-Path $VenvPath 'Scripts\python.exe'

Invoke-Checked $python @('-m', 'pip', 'install', '--disable-pip-version-check', '--timeout', '120', '--retries', '10', '--require-hashes',
    '--only-binary=:all:', '-r', (Join-Path $app 'requirements-dev.lock.txt'))
Invoke-Checked $python @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps',
    '--no-build-isolation', '-e', $app)
Invoke-Checked $python @('-m', 'meeting_minutes', 'doctor', '--no-capture')
