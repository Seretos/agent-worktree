# Run the test suite for the agent-worktree plugin.
#
# Sets up (or reuses) a project-local .venv, installs the plugin + test
# extras, then delegates to pytest. Any remaining arguments are forwarded
# to pytest as-is.
#
# Usage (from plugin root):
#   pwsh -File scripts/test.ps1
#   pwsh -File scripts/test.ps1 -v                  # verbose
#   pwsh -File scripts/test.ps1 tests/test_config.py # single file
#
# Requires: Python 3.11+ on PATH.

[CmdletBinding()]
param()

# Capture any extra args the caller passes after the script name.
$PytestArgs = $args

$root = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $root

# Note: do NOT set $ErrorActionPreference = "Stop" globally. PowerShell 5.1
# wraps native-command stderr as ErrorRecord, which trips Stop semantics for
# tools like pip that log to stderr. We check $LASTEXITCODE after each native
# call instead.

# PowerShell 5.1 on Windows lacks the automatic $IsWindows / $IsLinux
# variables PowerShell 7+ provides. Derive them ourselves.
if ($null -eq (Get-Variable -Name IsWindows -ErrorAction SilentlyContinue)) {
    $script:IsWindows = ($env:OS -eq "Windows_NT")
    $script:IsLinux   = -not $script:IsWindows
}

function Write-Step($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Fail($msg) {
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

# 1. Verify Python.
# In CI ($env:CI = "true") prefer `python` on PATH so we get the version that
# actions/setup-python installed. On Windows locally, prefer py.exe -3.
# On Linux, only `python` / `python3` exists.
Write-Step "Checking Python"
$script:PyCmd = $null
$script:PyArgs = @()

$preferPython = ($env:CI -eq "true")

if ($IsWindows -and -not $preferPython -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    $verRaw = & py.exe -3 --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $script:PyCmd = "py.exe"
        $script:PyArgs = @("-3")
        Write-Host "    $verRaw (via py.exe)"
    }
}
if (-not $script:PyCmd -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $verRaw = & python --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $script:PyCmd = "python"
        Write-Host "    $verRaw (via python)"
    }
}
if (-not $script:PyCmd -and (Get-Command python3 -ErrorAction SilentlyContinue)) {
    $verRaw = & python3 --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $script:PyCmd = "python3"
        Write-Host "    $verRaw (via python3)"
    }
}
if (-not $script:PyCmd) {
    Fail "No usable Python found. Install Python 3.11+."
}

function Invoke-Py {
    & $script:PyCmd @script:PyArgs @args
}

# 1b. Isolate plugin + test deps in a project-local virtualenv.
$venvDir = Join-Path $root ".venv"
if ($IsWindows) {
    $venvPy = Join-Path $venvDir "Scripts/python.exe"
} else {
    $venvPy = Join-Path $venvDir "bin/python"
}

# Detect cross-platform .venv contamination: a venv created under WSL/Linux
# and then "touched" by a Windows build (or vice versa) leaves pyvenv.cfg
# pointing at a foreign-OS interpreter. Purge the dir and rebuild fresh.
$venvCfg = Join-Path $venvDir "pyvenv.cfg"
if ((Test-Path $venvPy) -and (Test-Path $venvCfg)) {
    $cfgExec = (Select-String -Path $venvCfg -Pattern '^executable\s*=\s*(.+)$' `
                              -ErrorAction SilentlyContinue).Matches[0].Groups[1].Value
    if ($cfgExec) {
        $cfgExec = $cfgExec.Trim()
        $cfgIsWindowsPath = $cfgExec -match '^[A-Za-z]:[\\/]' -or $cfgExec -like '*\*'
        $cfgIsPosixPath   = $cfgExec.StartsWith('/')
        if ( ($IsWindows -and $cfgIsPosixPath) -or
             (-not $IsWindows -and $cfgIsWindowsPath) ) {
            Write-Step "Existing .venv is from a foreign OS ($cfgExec) -- purging"
            Remove-Item -Recurse -Force $venvDir -ErrorAction SilentlyContinue
        }
    }
}

if (-not (Test-Path $venvPy)) {
    Write-Step "Creating virtualenv at .venv/"
    Invoke-Py -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        if (-not $IsWindows) {
            Write-Host "    On Debian/Ubuntu, ensure python3-venv is installed:" -ForegroundColor Yellow
            Write-Host "      sudo apt install python3-venv" -ForegroundColor Yellow
        }
        Fail "Failed to create virtualenv at $venvDir."
    }
    if (-not (Test-Path $venvPy)) {
        Fail "venv was created but $venvPy is missing."
    }
}

# Rebind Python launcher to the venv.
$script:PyCmd = $venvPy
$script:PyArgs = @()
Write-Host "    Using $venvPy"

# Verify pip is present.
Invoke-Py -m pip --version > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Step "Bootstrapping pip in venv (ensurepip)"
    Invoke-Py -m ensurepip --upgrade --default-pip
    if ($LASTEXITCODE -ne 0) {
        if (-not $IsWindows) {
            Write-Host "    The venv has no pip and ensurepip cannot bootstrap it." -ForegroundColor Yellow
            Write-Host "    On Debian/Ubuntu, install the per-version venv package, e.g.:" -ForegroundColor Yellow
            Write-Host "      sudo apt install python3.12-venv python3-pip" -ForegroundColor Yellow
            Write-Host "    Then remove the broken .venv/ and re-run:" -ForegroundColor Yellow
            Write-Host "      rm -rf .venv && pwsh scripts/test.ps1" -ForegroundColor Yellow
        }
        Fail "venv has no pip and ensurepip failed."
    }
}

# 2. Install plugin + test deps.
Write-Step "Ensuring dependencies (plugin + test extras)"
Invoke-Py -m pip install --quiet --disable-pip-version-check -e ".[test]"
if ($LASTEXITCODE -ne 0) {
    Fail "pip install failed."
}

# 3. Run pytest.
Write-Step "Running pytest"
Invoke-Py -m pytest @PytestArgs
if ($LASTEXITCODE -ne 0) {
    Fail "Tests failed."
}

Write-Step "Done. All tests passed."
