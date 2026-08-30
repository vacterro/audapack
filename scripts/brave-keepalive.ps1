#Requires -Version 5.1
<#
.SYNOPSIS
    Brave Browser keep-alive launcher with background activity persistence.

.DESCRIPTION
    Launches Brave Browser with Chromium flags that prevent:
    - Background timer throttling (tab timers keep running when minimized)
    - Renderer backgrounding (JS/canvas keeps executing)
    - Occluded window throttling (hidden windows stay active)

.NOTES
    Place this alongside brave-portable.exe or adjust $BravePath below.
    Usage: powershell -ExecutionPolicy Bypass -File brave-keepalive.ps1
    Or: Right-click → "Run with PowerShell"
#>

# ── Configuration ──────────────────────────────────────────────────────
$BravePaths = @(
    "V:\___VAC\__P\__SOFT\_BRAVE\brave-portable.exe"
    "V:\___VAC\__P\__SOFT\_BRAVE\app\brave.exe"
    "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe"
    "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe"
    "$env:ProgramFiles(x86)\BraveSoftware\Brave-Browser\Application\brave.exe"
)

# Resolve Brave executable
$BravePath = $null
foreach ($candidate in $BravePaths) {
    if ($candidate -and (Test-Path -LiteralPath $candidate -ErrorAction SilentlyContinue)) {
        $BravePath = $candidate
        break
    }
}

if (-not $BravePath) {
    Write-Host "[ERROR] Brave Browser not found. Checked paths:" -ForegroundColor Red
    foreach ($p in $BravePaths) { Write-Host "  - $p" -ForegroundColor DarkGray }
    Write-Host "`nInstall Brave or update `$BravePaths in this script." -ForegroundColor Yellow
    exit 1
}

# ── Keep-Alive Flags ───────────────────────────────────────────────────
# These Chromium flags disable all background throttling mechanisms:
$KeepAliveFlags = @(
    # CRITICAL: Prevents JavaScript timers from being throttled in background tabs
    "--disable-background-timer-throttling"

    # CRITICAL: Prevents renderer process from being paused when window is occluded
    "--disable-backgrounding-occluded-windows"

    # CRITICAL: Prevents renderer from being backgrounded entirely
    "--disable-renderer-backgrounding"

    # Prevents Brave from discarding tabs under memory pressure
    "--disable-features=TabDiscarding"

    # Skip first-run experience and default browser check (faster startup)
    "--no-first-run"
    "--no-default-browser-check"
)

# ── User Profile ───────────────────────────────────────────────────────
# Persist profile in a known location so state survives restarts
$ProfileDir = "$env:LOCALAPPDATA\AUDAPACK\brave-profile"
if (-not (Test-Path -LiteralPath $ProfileDir)) {
    New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null
}

# ── Launch ──────────────────────────────────────────────────────────────
$LaunchArgs = @(
    "--user-data-dir=`"$ProfileDir`""
) + $KeepAliveFlags

# Append any extra arguments passed to this script
$LaunchArgs += $args

Write-Host "[Brave Keep-Alive] Launching: $BravePath" -ForegroundColor Green
Write-Host "[Brave Keep-Alive] Profile:   $ProfileDir" -ForegroundColor DarkGray
Write-Host "[Brave Keep-Alive] Flags:     background-timer-throttling=OFF, renderer-backgrounding=OFF" -ForegroundColor DarkGray

# Start without waiting (detached) — Brave keeps running after this script exits
$process = Start-Process -FilePath $BravePath `
    -ArgumentList $LaunchArgs `
    -PassThru `
    -ErrorAction Stop

Write-Host "[Brave Keep-Alive] Started PID: $($process.Id)" -ForegroundColor Green
