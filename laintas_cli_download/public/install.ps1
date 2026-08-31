$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$BaseUrl = "https://cli.laintas.com"
$Asset = "laintas-cli_windows_amd64.zip"
$TempDir = Join-Path ([IO.Path]::GetTempPath()) ("laintas-install-" + [Guid]::NewGuid().ToString("N"))
$Archive = Join-Path $TempDir $Asset
$Extracted = Join-Path $TempDir "package"

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Laintas CLI for Windows requires 64-bit Windows."
}

try {
    New-Item -ItemType Directory -Path $TempDir -Force | Out-Null
    Write-Host ""
    Write-Host "-- Laintas CLI Windows Installer ---------------------------"
    Write-Host "Downloading $Asset..."

    Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/releases/latest/$Asset" -OutFile $Archive
    $checksums = (Invoke-WebRequest -UseBasicParsing -Uri "$BaseUrl/releases/latest/SHA256SUMS.txt").Content
    $line = @($checksums -split "`r?`n" | Where-Object { $_ -match ("\s+\*?" + [Regex]::Escape($Asset) + "$") })
    if ($line.Count -ne 1) {
        throw "SHA256SUMS.txt does not contain exactly one checksum for $Asset."
    }
    $expected = ($line[0] -split "\s+")[0].ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "Checksum mismatch for $Asset."
    }

    Expand-Archive -LiteralPath $Archive -DestinationPath $Extracted -Force
    $installer = Join-Path $Extracted "install.ps1"
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "The Windows archive is invalid: install.ps1 is missing."
    }
    & $installer
} finally {
    if (Test-Path -LiteralPath $TempDir) {
        Remove-Item -LiteralPath $TempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
