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

# --- Add to user PATH if not already present ------------------------------

$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if (-not $userPath) { $userPath = "" }
$pathEntries = $userPath -split ";" | Where-Object { $_ -ne "" }
if ($pathEntries -notcontains $installDir) {
    $newPath = if ($userPath) { "$installDir;$userPath" } else { $installDir }
    [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
    Write-Host ""
    Write-Host "Added $installDir to your user PATH." -ForegroundColor Yellow
    Write-Host "Open a new PowerShell window for the change to take effect." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Aries Mesh installed to $binaryPath" -ForegroundColor Green
Write-Host ""
Write-Host "Get started:" -ForegroundColor Cyan
Write-Host "  aries init --name $env:COMPUTERNAME"
Write-Host "  aries start"
Write-Host ""
Write-Host "Then open http://localhost:7272 for the dashboard."
Write-Host ""
