[CmdletBinding()]
param(
    [string]$DistroName = "Laintas-CLI",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Laintas")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

function Get-RegisteredDistributions {
    $output = & wsl.exe --list --quiet 2>$null
    if ($LASTEXITCODE -ne 0) {
        return @()
    }
    return @($output | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ })
}

function Add-UserPath([string]$Directory) {
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($current -split ";" | Where-Object { $_ })
    if ($parts -notcontains $Directory) {
        $updated = (@($parts) + $Directory) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $updated, "User")
        $env:Path = "$env:Path;$Directory"
        return $true
    }
    return $false
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "WSL is not installed. Run 'wsl --install --no-distribution' as Administrator, restart Windows, then run this installer again."
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Laintas CLI for Windows requires 64-bit Windows."
}

$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Rootfs = Join-Path $PackageRoot "laintas-rootfs.tar.gz"
$BundledLauncher = Join-Path $PackageRoot "laintas-cli.exe"
$BinDir = Join-Path $InstallRoot "bin"
$DistroDir = Join-Path $InstallRoot "WSL"
$InstalledLauncher = Join-Path $BinDir "laintas-cli.exe"

if (-not (Test-Path -LiteralPath $BundledLauncher -PathType Leaf)) {
    throw "The Windows package is incomplete: laintas-cli.exe is missing."
}

$registered = Get-RegisteredDistributions
if ($registered -notcontains $DistroName) {
    if (-not (Test-Path -LiteralPath $Rootfs -PathType Leaf)) {
        throw "The Windows package is incomplete: laintas-rootfs.tar.gz is missing."
    }
    if (Test-Path -LiteralPath $DistroDir) {
        $existing = @(Get-ChildItem -LiteralPath $DistroDir -Force -ErrorAction SilentlyContinue)
        if ($existing.Count -gt 0) {
            throw "The WSL target directory is not empty: $DistroDir"
        }
    } else {
        New-Item -ItemType Directory -Path $DistroDir -Force | Out-Null
    }

    Write-Host "Importing the private $DistroName WSL environment..."
    & wsl.exe --import $DistroName $DistroDir $Rootfs --version 2
    if ($LASTEXITCODE -ne 0) {
        throw "WSL import failed with exit code $LASTEXITCODE."
    }
} else {
    Write-Host "$DistroName is already installed; preserving its Linux filesystem and user data."
}

New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
Copy-Item -LiteralPath $BundledLauncher -Destination $InstalledLauncher -Force

Write-Host "Verifying the Laintas runtime..."
& wsl.exe --distribution $DistroName --user root --exec test -x /usr/local/bin/laintas-cli
if ($LASTEXITCODE -ne 0) {
    throw "The imported distribution does not contain /usr/local/bin/laintas-cli."
}

$pathChanged = Add-UserPath $BinDir

$Programs = [Environment]::GetFolderPath("Programs")
if ($Programs) {
    $ShortcutPath = Join-Path $Programs "Laintas CLI.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $InstalledLauncher
    $Shortcut.WorkingDirectory = [Environment]::GetFolderPath("UserProfile")
    $Shortcut.Description = "Laintas CLI"
    $Shortcut.Save()
}

Write-Host ""
Write-Host "Laintas CLI was installed successfully." -ForegroundColor Green
Write-Host "Launcher: $InstalledLauncher"
if ($pathChanged) {
    Write-Host "Open a new terminal, then run: laintas-cli"
} else {
    Write-Host "Run: laintas-cli"
}
