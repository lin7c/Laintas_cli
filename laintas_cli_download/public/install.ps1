$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$BaseUrl = "https://cli.laintas.com"
$Asset = "laintas-cli_windows_amd64_setup.exe"
$TempDir = Join-Path ([IO.Path]::GetTempPath()) ("laintas-install-" + [Guid]::NewGuid().ToString("N"))
$Installer = Join-Path $TempDir $Asset

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Laintas CLI for Windows requires 64-bit Windows."
}
if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {
    throw "The current Windows package supports x86_64 only; ARM64 packaging is not available yet."
}

try {
    New-Item -ItemType Directory -Path $TempDir -Force | Out-Null
    Write-Host ""
    Write-Host "-- Laintas CLI Windows Installer ---------------------------"
    Write-Host "Downloading $Asset..."

    Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/releases/latest/$Asset" -OutFile $Installer
    $checksums = (Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/releases/latest/SHA256SUMS.txt").Content
    $line = @($checksums -split "`r?`n" | Where-Object { $_ -match ("\s+\*?" + [Regex]::Escape($Asset) + "$") })
    if ($line.Count -ne 1) {
        throw "SHA256SUMS.txt does not contain exactly one checksum for $Asset."
    }
    $expected = ($line[0] -split "\s+")[0].ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "Checksum mismatch for $Asset."
    }

    # Keep the one-line bootstrap command, but show the real installer so the
    # user can choose a drive and decide whether to launch when it finishes.
    $process = Start-Process -FilePath $Installer -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "The Windows installer failed with exit code $($process.ExitCode)."
    }
} finally {
    if (Test-Path -LiteralPath $TempDir) {
        Remove-Item -LiteralPath $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
