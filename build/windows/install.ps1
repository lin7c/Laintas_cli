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

function Get-DistributionBasePath([string]$Name) {
    $registry = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss"
    if (-not (Test-Path -LiteralPath $registry)) {
        return $null
    }
    foreach ($key in Get-ChildItem -LiteralPath $registry -ErrorAction SilentlyContinue) {
        $values = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
        if ($values.DistributionName -eq $Name -and $values.BasePath) {
            return ([string]$values.BasePath -replace '^\\\\\?\\', '')
        }
    }
    return $null
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
$BundledIcon = Join-Path $PackageRoot "icon.ico"
$BundledFragment = Join-Path $PackageRoot "terminal-fragment.json"
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
    $registeredBase = Get-DistributionBasePath $DistroName
    if ($registeredBase) {
        $selectedBase = [IO.Path]::GetFullPath($DistroDir).TrimEnd('\')
        $registeredBase = [IO.Path]::GetFullPath($registeredBase).TrimEnd('\')
        if (-not $selectedBase.Equals($registeredBase, [StringComparison]::OrdinalIgnoreCase)) {
            throw "$DistroName is already installed at $registeredBase. Keep its existing installation directory ($([IO.Path]::GetDirectoryName($registeredBase))) when upgrading."
        }
    }
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

# The icon has to exist as a file on disk, not only inside the executable:
# a Windows Terminal profile references it by path.
$InstalledIcon = Join-Path $BinDir "laintas-cli.ico"
if (Test-Path -LiteralPath $BundledIcon -PathType Leaf) {
    Copy-Item -LiteralPath $BundledIcon -Destination $InstalledIcon -Force
}

# A Windows Terminal fragment, which is how an application adds a profile
# without editing the user's settings.json. Windows Terminal reads this
# directory whether or not it is installed today, so writing it is worth
# doing even on a machine that has only the legacy console: it starts
# working the day the user installs Windows Terminal.
$terminalProfile = $false
if (Test-Path -LiteralPath $BundledFragment -PathType Leaf) {
    try {
        $FragmentDir = Join-Path $env:LOCALAPPDATA "Microsoft\Windows Terminal\Fragments\Laintas.LaintasCLI"
        New-Item -ItemType Directory -Path $FragmentDir -Force | Out-Null
        $fragment = Get-Content -LiteralPath $BundledFragment -Raw -Encoding UTF8
        # JSON string values: a Windows path's backslashes have to be escaped
        # or the profile silently fails to parse and never appears.
        $fragment = $fragment.Replace("__LAUNCHER__", $InstalledLauncher.Replace("\", "\\"))
        $fragment = $fragment.Replace("__ICON__", $InstalledIcon.Replace("\", "\\"))
        $FragmentPath = Join-Path $FragmentDir "laintas-cli.json"
        # No BOM: Windows Terminal rejects a fragment that starts with one.
        [IO.File]::WriteAllText($FragmentPath, $fragment, (New-Object Text.UTF8Encoding $false))
        $terminalProfile = $true
    } catch {
        Write-Host "Could not register the Windows Terminal profile: $($_.Exception.Message)"
    }
}

$Programs = [Environment]::GetFolderPath("Programs")
if ($Programs) {
    $ShortcutPath = Join-Path $Programs "Laintas CLI.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    # Point the shortcut at Windows Terminal when it is available, so the
    # profile's font, colours and mouse handling apply from the first launch.
    # Starting the launcher directly gets whatever terminal Windows defaults
    # to, which on Windows 10 is the legacy console.
    $wt = Get-Command wt.exe -ErrorAction SilentlyContinue
    if ($terminalProfile -and $wt) {
        $Shortcut.TargetPath = $wt.Source
        $Shortcut.Arguments = '-p "Laintas CLI"'
    } else {
        $Shortcut.TargetPath = $InstalledLauncher
    }
    $Shortcut.WorkingDirectory = [Environment]::GetFolderPath("UserProfile")
    $Shortcut.Description = "Laintas CLI"
    if (Test-Path -LiteralPath $InstalledIcon -PathType Leaf) {
        $Shortcut.IconLocation = $InstalledIcon
    }
    $Shortcut.Save()
}

Write-Host ""
Write-Host "Laintas CLI was installed successfully." -ForegroundColor Green
Write-Host "Launcher: $InstalledLauncher"
if ($terminalProfile) {
    Write-Host "Windows Terminal profile: Laintas CLI"
} else {
    Write-Host "Install Windows Terminal for the full interface (colours, mouse, box drawing)."
}
if ($pathChanged) {
    Write-Host "Open a new terminal, then run: laintas-cli"
} else {
    Write-Host "Run: laintas-cli"
}
