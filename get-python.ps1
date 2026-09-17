# Getting a Python onto a machine that has none — the one part that cannot
# be written in Python. Deliberately small: everything else lives in
# setup.py, where it can be tested and where failures explain themselves.
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "  I can install Python 3.12 for you:" -ForegroundColor White
Write-Host "    - via winget if this PC has it, or" -ForegroundColor DarkGray
Write-Host "    - the official installer from python.org" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Nothing else is installed and nothing is sent anywhere." -ForegroundColor DarkGray
Write-Host ""
$a = Read-Host "  Install Python now? (y/n)"
if ($a -notmatch '^(y|yes)$') {
    Write-Host ""
    Write-Host "  No problem - nothing was changed." -ForegroundColor Yellow
    Write-Host "  Install Python 3.12 from https://www.python.org/downloads/"
    Write-Host "  (tick 'Add python.exe to PATH'), then run install.bat again."
    exit 1
}

$done = $false
if (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Host "  Installing via winget..." -ForegroundColor Cyan
    try {
        winget install --id Python.Python.3.12 --silent `
            --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) { $done = $true }
    } catch {
        Write-Host "  winget didn't manage it; trying python.org." -ForegroundColor Yellow
    }
}

if (-not $done) {
    # 64-bit and ARM64 need different installers; picking the wrong one
    # fails in a way that looks like a corrupt download
    $arch = $env:PROCESSOR_ARCHITECTURE
    $file = if ($arch -eq "ARM64") { "python-3.12.7-arm64.exe" }
            else { "python-3.12.7-amd64.exe" }
    $url = "https://www.python.org/ftp/python/3.12.7/$file"
    $tmp = Join-Path $env:TEMP $file
    Write-Host "  Downloading Python for $arch..." -ForegroundColor Cyan
    try {
        # some corporate proxies need the system settings used explicitly
        $wc = New-Object System.Net.WebClient
        $wc.Proxy = [System.Net.WebRequest]::GetSystemWebProxy()
        $wc.Proxy.Credentials = [System.Net.CredentialCache]::DefaultCredentials
        $wc.DownloadFile($url, $tmp)
    } catch {
        Write-Host ""
        Write-Host "  Could not download Python." -ForegroundColor Red
        Write-Host "  $($_.Exception.Message)" -ForegroundColor DarkGray
        Write-Host "  Install it by hand from https://www.python.org/downloads/"
        Write-Host "  and tick 'Add python.exe to PATH', then run install.bat again."
        exit 1
    }
    Write-Host "  Running the installer (about a minute)..." -ForegroundColor Cyan
    Start-Process -FilePath $tmp -Wait -ArgumentList `
        "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1", "Include_launcher=1"
    Remove-Item $tmp -ErrorAction SilentlyContinue
}

# this window won't see the new PATH; refresh it so the caller can continue
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")
Write-Host "  Python installed." -ForegroundColor Green
exit 0
