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
if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {
    throw "This package is for x86_64 Windows. Windows ARM64 packaging is not available yet."
}

$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Rootfs = Join-Path $PackageRoot "laintas-rootfs.tar.gz"
$LinuxBinary = Join-Path $PackageRoot "laintas-cli-linux"
$BundledLauncher = Join-Path $PackageRoot "laintas-cli.exe"
$BinDir = Join-Path $InstallRoot "bin"
$DistroDir = Join-Path $InstallRoot "WSL"
$InstalledLauncher = Join-Path $BinDir "laintas-cli.exe"

if (-not (Test-Path -LiteralPath $BundledLauncher -PathType Leaf)) {
    throw "The Windows package is incomplete: laintas-cli.exe is missing."
}
if (-not (Test-Path -LiteralPath $LinuxBinary -PathType Leaf)) {
    throw "The Windows installer is incomplete: the Linux runtime is missing."
}

$registered = Get-RegisteredDistributions
$importedNow = $false
if ($registered -notcontains $DistroName) {
    if (-not (Test-Path -LiteralPath $Rootfs -PathType Leaf)) {
        throw "The Windows package is incomplete: laintas-rootfs.tar.gz is missing."
    }
    $createdDistroDir = $false
    if (Test-Path -LiteralPath $DistroDir) {
        $existing = @(Get-ChildItem -LiteralPath $DistroDir -Force -ErrorAction SilentlyContinue)
        if ($existing.Count -gt 0) {
            throw "The WSL target directory is not empty: $DistroDir"
        }
    } else {
        New-Item -ItemType Directory -Path $DistroDir -Force | Out-Null
        $createdDistroDir = $true
    }

    Write-Host "Importing the private $DistroName WSL environment..."
    try {
        & wsl.exe --import $DistroName $DistroDir $Rootfs --version 2
        if ($LASTEXITCODE -ne 0) {
            throw "WSL import failed with exit code $LASTEXITCODE."
        }
        $importedNow = $true
    } catch {
        # Only remove a directory this invocation created, and only when WSL
        # did not register the distribution. Never delete an existing distro.
        if ($createdDistroDir -and ((Get-RegisteredDistributions) -notcontains $DistroName)) {
            Remove-Item -LiteralPath $DistroDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw
    }
} else {
    Write-Host "$DistroName is already installed; preserving its Linux filesystem and user data."
}

New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
Copy-Item -LiteralPath $BundledLauncher -Destination $InstalledLauncher -Force

Write-Host "Installing the current Laintas runtime without replacing Linux user data..."
$fullLinuxBinary = [IO.Path]::GetFullPath($LinuxBinary)
if ($fullLinuxBinary -notmatch '^([A-Za-z]):\\(.*)$') {
    throw "Cannot translate the installer runtime path for WSL: $fullLinuxBinary"
}
$drive = $Matches[1].ToLowerInvariant()
$relative = $Matches[2] -replace '\\', '/'
$wslLinuxBinary = "/mnt/$drive/$relative"
& wsl.exe --distribution $DistroName --user root --exec /usr/bin/install -m 0755 $wslLinuxBinary /usr/local/bin/laintas-cli
if ($LASTEXITCODE -ne 0) {
    if ($importedNow) {
        & wsl.exe --unregister $DistroName 2>$null | Out-Null
    }
    throw "Could not install the Laintas runtime inside $DistroName."
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
