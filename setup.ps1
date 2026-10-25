# Setup for garmin-mcp on Windows. Installs what is missing, then logs in to
# Garmin and registers the server with Claude.
#
# From a fresh PC, in PowerShell:
#   irm https://raw.githubusercontent.com/martinbo94/garmin-mcp/main/setup.ps1 | iex
# Or from inside a clone:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
# Safe to re-run: every step skips what is already done.

# Errors use throw, never exit: under irm | iex, exit would close the PowerShell window
$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/martinbo94/garmin-mcp.git"
$PythonVersion = "3.12"

function Update-PathFromRegistry {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

Write-Host ""
Write-Host "=== garmin-mcp setup ==="
Write-Host ""

# 1. Find the repo, or install git and clone it when run through irm | iex
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "server.py"))) {
    $Repo = $PSScriptRoot
} else {
    $Repo = Join-Path $HOME "garmin-mcp"
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "git is missing and winget is not available. Install git from https://git-scm.com/download/win and run this again."
        }
        Write-Host "Installing git..."
        winget install --id Git.Git -e --source winget --silent --accept-package-agreements --accept-source-agreements | Out-Null
        Update-PathFromRegistry
        Write-Host "  Done."
        Write-Host ""
    }
    if (Test-Path (Join-Path $Repo ".git")) {
        Write-Host "Updating $Repo..."
        git -C $Repo pull --ff-only
    } else {
        Write-Host "Downloading garmin-mcp to $Repo..."
        git clone $RepoUrl $Repo
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not download garmin-mcp." }
    Write-Host ""
}

# 2. uv, which downloads Python itself — no python.org installer needed
$Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $Uv) {
    $Uv = Join-Path $HOME ".local\bin\uv.exe"
    if (-not (Test-Path $Uv)) {
        Write-Host "Installing uv (Python installer)..."
        powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
        Write-Host "  Done."
        Write-Host ""
    }
}

# 3. Python + dependencies in .venv
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
Write-Host "Installing Python $PythonVersion and dependencies..."
$hasPython = $false
if (Test-Path $Python) {
    & $Python -c "import sys; sys.exit(sys.version_info < (3, 10))"
    $hasPython = ($LASTEXITCODE -eq 0)
}
if (-not $hasPython) {
    & $Uv venv --quiet --clear --python $PythonVersion (Join-Path $Repo ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not install Python." }
}
& $Uv pip install --quiet --python $Python -r (Join-Path $Repo "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install the Python dependencies." }
Write-Host "  Done."
Write-Host ""

# 4. Garmin login, .env and Claude registration
& $Python (Join-Path $Repo "setup_helper.py")
if ($LASTEXITCODE -ne 0) { throw "Setup stopped (see the message above). Fix it and run this again." }

Write-Host ""
Write-Host "=== Setup complete ==="
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Quit Claude Desktop completely (right-click its tray icon > Quit) and open it again"
Write-Host "  2. Start a new conversation and say: 'Let's set up my profile'"
Write-Host ""
