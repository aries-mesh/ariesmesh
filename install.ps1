# Aries Mesh installer for Windows.
#
# Usage:
#   irm https://raw.githubusercontent.com/aries-mesh/ariesmesh/main/install.ps1 | iex
#
# Downloads the latest aries-windows-amd64.exe, installs it under
# %LOCALAPPDATA%\aries, and adds that directory to the user PATH if needed.

$ErrorActionPreference = "Stop"

$repo        = "aries-mesh/ariesmesh"
$assetName   = "aries-windows-amd64.exe"
$installDir  = Join-Path $env:LOCALAPPDATA "aries"
$binaryPath  = Join-Path $installDir "aries.exe"

Write-Host ""
Write-Host "Aries Mesh installer" -ForegroundColor Cyan
Write-Host "  Platform: Windows x64"
Write-Host "  Install:  $installDir"
Write-Host ""

# --- Find the latest release asset ----------------------------------------

$releaseUrl = "https://api.github.com/repos/$repo/releases/latest"
try {
    $release = Invoke-RestMethod -Uri $releaseUrl -Headers @{ "User-Agent" = "aries-installer" }
} catch {
    Write-Host "Error: could not fetch the latest release from GitHub." -ForegroundColor Red
    Write-Host "Check https://github.com/$repo/releases/latest manually."
    Write-Host $_.Exception.Message -ForegroundColor DarkGray
    exit 1
}

$asset = $release.assets | Where-Object { $_.name -eq $assetName }
if (-not $asset) {
    Write-Host "Error: could not find '$assetName' in the latest release." -ForegroundColor Red
    Write-Host "Available assets:"
    foreach ($a in $release.assets) { Write-Host "  - $($a.name)" -ForegroundColor DarkGray }
    exit 1
}

$downloadUrl = $asset.browser_download_url
Write-Host "Downloading from: $downloadUrl"

# --- Prepare install directory --------------------------------------------

if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
}

# --- Download to a temp file, then move into place ------------------------

$tmpFile = [System.IO.Path]::GetTempFileName()
try {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $tmpFile -UseBasicParsing
    Move-Item -Path $tmpFile -Destination $binaryPath -Force
} catch {
    Write-Host "Error: download failed." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor DarkGray
    if (Test-Path $tmpFile) { Remove-Item -Force $tmpFile -ErrorAction SilentlyContinue }
    exit 1
}

# --- Persist install dir to the user PATH (registry) so future shells see it
# We only touch the registry when the entry is missing, but we ALWAYS patch
# the live $env:PATH below — because the registry write doesn't reach the
# already-running PowerShell session, and a user who pipes us through `iex`
# expects `aries` to be on PATH immediately.

$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if (-not $userPath) { $userPath = "" }
$pathEntries = $userPath -split ";" | Where-Object { $_ -ne "" }
if ($pathEntries -notcontains $installDir) {
    $newPath = if ($userPath) { "$installDir;$userPath" } else { $installDir }
    [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
    Write-Host ""
    Write-Host "Added $installDir to your user PATH." -ForegroundColor Yellow
}

# --- Patch the CURRENT shell so `aries` resolves right away
# `irm | iex` runs this script in-process; mutating $env:PATH persists for
# the rest of the user's session, no terminal restart needed.
$liveEntries = $env:PATH -split ";" | Where-Object { $_ -ne "" }
if ($liveEntries -notcontains $installDir) {
    $env:PATH = "$installDir;$env:PATH"
}

Write-Host ""
Write-Host "Aries Mesh installed to $binaryPath" -ForegroundColor Green
Write-Host ""

if (Get-Command aries -ErrorAction SilentlyContinue) {
    Write-Host "Get started:" -ForegroundColor Cyan
    Write-Host "  aries init --name $env:COMPUTERNAME"
    Write-Host "  aries start"
} else {
    # Defensive: should not happen now that $env:PATH is patched in-process,
    # but PowerShell's command-lookup cache can be quirky on edge cases
    # (e.g. when the script is dot-sourced into a constrained-language host).
    Write-Host "The 'aries' command isn't resolving in this session yet." -ForegroundColor Yellow
    Write-Host "Run it directly with the full path:"
    Write-Host "  & `"$binaryPath`" init --name $env:COMPUTERNAME"
    Write-Host "Or open a new PowerShell window."
}

Write-Host ""
Write-Host "Then open http://localhost:7272 for the dashboard."
Write-Host ""
