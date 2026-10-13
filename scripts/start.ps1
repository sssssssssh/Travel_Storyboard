# Travel Storyboard v2.0 — one-click launcher (Windows PowerShell)
#
#   .\scripts\start.ps1                 # install if needed, build UI, then run
#   .\scripts\start.ps1 -SkipFrontend   # backend only (use `pnpm dev` for the UI)
#   .\scripts\start.ps1 -Port 8800
#
# Safe to re-run: every step is idempotent. Nothing is written outside
# backend\.venv, backend\data, frontend\node_modules and frontend\dist.

[CmdletBinding()]
param(
    [int]$Port = 8737,
    [string]$BindHost = "127.0.0.1",
    [switch]$SkipInstall,
    [switch]$SkipFrontend,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$backend  = Join-Path $repoRoot "backend"
$frontend = Join-Path $repoRoot "frontend"
$venvPy   = Join-Path $backend ".venv\Scripts\python.exe"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    $msg" -ForegroundColor Yellow }

function Resolve-Python {
    foreach ($candidate in @("py", "python", "python3")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $version = & $cmd.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if (-not $version) { continue }
        $parts = $version.Split(".")
        if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) {
            return @{ Exe = $cmd.Source; Version = $version }
        }
        Write-Warn2 "$candidate is Python $version (need 3.11+)"
    }
    return $null
}

Write-Host "Travel Storyboard v2.0" -ForegroundColor White
Write-Host "Repository: $repoRoot"

# ---------------------------------------------------------------- prerequisites
Write-Step "Checking prerequisites"

$python = Resolve-Python
if (-not $python) {
    throw "Python 3.11+ not found. Install it from https://www.python.org/downloads/ (tick 'Add to PATH')."
}
Write-Ok "Python $($python.Version) at $($python.Exe)"

# FFmpeg: on PATH or via backend\.env (TRAVELSB_FFMPEG_PATH / TRAVELSB_FFPROBE_PATH)
$ffmpegOnPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
$envFile = Join-Path $backend ".env"
$ffmpegConfigured = $false
if (Test-Path $envFile) {
    $ffmpegConfigured = (Select-String -Path $envFile -Pattern "^\s*TRAVELSB_FFMPEG_PATH\s*=" -Quiet)
}
if ($ffmpegOnPath) {
    Write-Ok "ffmpeg found on PATH: $($ffmpegOnPath.Source)"
} elseif ($ffmpegConfigured) {
    Write-Ok "ffmpeg configured through backend\.env"
} else {
    Write-Warn2 "ffmpeg not found on PATH and not set in backend\.env"
    Write-Warn2 "Scanning/detection need it. Either add ffmpeg to PATH, or create"
    Write-Warn2 "backend\.env with:  TRAVELSB_FFMPEG_PATH=D:\ffmpeg\bin\ffmpeg.exe"
    Write-Warn2 "and                TRAVELSB_FFPROBE_PATH=D:\ffmpeg\bin\ffprobe.exe"
}

$pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
$npm  = Get-Command npm  -ErrorAction SilentlyContinue

# ------------------------------------------------------------------- venv + deps
if (-not $SkipInstall) {
    Write-Step "Preparing Python environment"
    if (-not (Test-Path $venvPy)) {
        Write-Host "    creating backend\.venv"
        & $python.Exe -m venv (Join-Path $backend ".venv")
    }
    Write-Host "    installing requirements (this can take a few minutes the first time)"
    & $venvPy -m pip install --upgrade pip --quiet
    & $venvPy -m pip install -r (Join-Path $backend "requirements.txt") --quiet
    Write-Ok "Python dependencies ready"
} else {
    if (-not (Test-Path $venvPy)) { throw "-SkipInstall was given but backend\.venv does not exist." }
    Write-Ok "Skipping dependency install"
}

# ------------------------------------------------------------------- frontend build
if (-not $SkipFrontend) {
    Write-Step "Building the web UI"
    $distIndex = Join-Path $frontend "dist\index.html"
    if (-not $pnpm -and -not $npm) {
        Write-Warn2 "Neither pnpm nor npm found — skipping the UI build."
        Write-Warn2 "Install Node.js 18+ from https://nodejs.org/ then re-run."
        if (-not (Test-Path $distIndex)) {
            throw "No prebuilt UI in frontend\dist and no package manager available."
        }
    } else {
        $pm = if ($pnpm) { "pnpm" } else { "npm" }
        if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
            Write-Host "    installing frontend dependencies with $pm"
            & $pm install --prefix $frontend
        } else {
            Write-Host "    frontend dependencies already installed"
        }
        Write-Host "    building"
        & $pm run build --prefix $frontend
        Write-Ok "UI built at frontend\dist"
    }
} else {
    Write-Ok "Skipping UI build"
}

# --------------------------------------------------------------------------- run
Write-Step "Starting the server"

$dataDir = Join-Path $backend "data"
$tokenFile = Join-Path $dataDir "token.txt"

$env:TRAVELSB_BIND_HOST = $BindHost
$env:TRAVELSB_BIND_PORT = "$Port"

$uvicorn = Join-Path $backend ".venv\Scripts\uvicorn.exe"
if (-not (Test-Path $uvicorn)) { $uvicorn = $null }

if (-not $NoBrowser) {
    # Wait for the token file / port, then open the browser with the token.
    Start-Job -ScriptBlock {
        param($port, $tokenFile)
        for ($i = 0; $i -lt 120; $i++) {
            Start-Sleep -Milliseconds 500
            if (Test-Path $tokenFile) {
                try {
                    $t = (Get-Content $tokenFile -Raw).Trim()
                    if ($t) {
                        Start-Sleep -Milliseconds 700
                        Start-Process "http://127.0.0.1:$port/?token=$t"
                        return
                    }
                } catch { }
            }
        }
    } -ArgumentList $Port, $tokenFile | Out-Null
    Write-Host "    the browser will open automatically once the server is ready"
}

Write-Host ""
Write-Host "    UI ............ http://$BindHost`:$Port/"
Write-Host "    API docs ...... http://$BindHost`:$Port/docs"
Write-Host "    data dir ...... $dataDir"
Write-Host "    token file .... $tokenFile"
Write-Host ""
Write-Host "    Press Ctrl+C to stop." -ForegroundColor Yellow
Write-Host ""

Push-Location $backend
try {
    if ($uvicorn) {
        & $uvicorn app.main:app --host $BindHost --port $Port
    } else {
        & $venvPy -m uvicorn app.main:app --host $BindHost --port $Port
    }
} finally {
    Pop-Location
    Get-Job | Remove-Job -Force -ErrorAction SilentlyContinue
}
