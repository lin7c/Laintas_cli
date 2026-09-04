[CmdletBinding()]
param(
    [string]$DistroName = "Laintas-CLI",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Laintas")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

# Where a failed install leaves its evidence. The installer window scrolls and
# then closes; these two files are what the user still has afterwards, and the
# one-line .txt is what the NSIS message box actually shows. Anything thrown
# below therefore has to read as the real reason, addressed to the user.
$LogFile = Join-Path $env:TEMP "laintas-cli-install.log"
$ErrorFile = Join-Path $env:TEMP "laintas-cli-install-error.txt"
Remove-Item -LiteralPath $ErrorFile -Force -ErrorAction SilentlyContinue
try { Start-Transcript -LiteralPath $LogFile -Force | Out-Null } catch { }

# Exit codes. NSIS prints whichever one it gets; a support log can tell the
# classes apart without parsing English.
$EXIT_OTHER = 1
$EXIT_WSL = 10
$EXIT_UNSUPPORTED_HOST = 11
$EXIT_PACKAGE_INCOMPLETE = 12
$EXIT_INSTALL_STATE = 13

$script:FailureCode = $EXIT_OTHER

function Fail {
    param([string]$Message, [int]$Code = 1)
    $script:FailureCode = $Code
    throw $Message
}

trap {
    $message = ($_.Exception.Message -replace "`r?`n", " ").Trim()
    if (-not $message) { $message = "Unknown installation error." }
    try {
        # The system ANSI code page, not UTF-8: NSIS reads this file with
        # FileRead, which decodes with the ANSI code page, so a path holding
        # the user's non-Latin account name only survives if both sides agree.
        [IO.File]::WriteAllText($ErrorFile, $message, [Text.Encoding]::Default)
    } catch { }
    Write-Host ""
    Write-Host "Installation failed: $message"
    Write-Host "Log: $LogFile"
    try { Stop-Transcript | Out-Null } catch { }
    exit $script:FailureCode
}

function Invoke-Wsl {
    # wsl.exe writes UTF-16LE, which arrives here as text with a NUL between
    # every character. Its exit code alone says nothing useful, so the output
    # is captured too: the HRESULT inside it is the only part of a WSL failure
    # that is not localised, and it is what names the actual cause.
    # Merging stderr into the output is what makes the HRESULT readable, and
    # it is also what makes this assignment necessary: with the script's
    # "Stop" preference in force, a native command writing to a redirected
    # stderr raises a terminating NativeCommandError, so reading WSL's own
    # error message would itself abort the install. The assignment is
    # function-scoped and gone on return.
    $ErrorActionPreference = "Continue"
    $raw = & wsl.exe @args 2>&1
    $code = $LASTEXITCODE
    $text = (@($raw) | ForEach-Object { ($_ | Out-String) -replace "`0", "" }) -join ""
    return [pscustomobject]@{ ExitCode = $code; Output = $text.Trim() }
}

function Get-WslDiagnosis([string]$Output) {
    $advice = [ordered]@{
        "0x8007019e" = "The Windows Subsystem for Linux is not enabled on this machine. Open PowerShell as Administrator, run 'wsl --install --no-distribution', restart Windows, then run this installer again."
        "0x80370102" = "Hardware virtualisation is off. Enable the 'Virtual Machine Platform' Windows feature and turn on virtualisation (Intel VT-x / AMD-V) in the BIOS/UEFI, then run this installer again."
        "0x80370114" = "WSL 2 could not start its virtual machine. Enable the 'Virtual Machine Platform' Windows feature and check that virtualisation is on in the BIOS/UEFI and not held by another hypervisor."
        "0x800701bc" = "The WSL 2 kernel is missing or out of date. Open PowerShell as Administrator, run 'wsl --update', then run this installer again."
        "0x80070422" = "A Windows service that WSL needs is disabled. Set the 'LxssManager' service to Manual or Automatic, then run this installer again."
        "0x80070005" = "Windows denied access while creating the WSL distribution. Check that no security software is blocking WSL, then run this installer again."
    }
    foreach ($code in $advice.Keys) {
        if ($Output -match [Regex]::Escape($code)) {
            return "$($advice[$code]) (WSL reported $code.)"
        }
    }
    return $null
}

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
    Fail "WSL is not installed on this machine. Open PowerShell as Administrator, run 'wsl --install --no-distribution', restart Windows, then run this installer again." $EXIT_WSL
}

if (-not [Environment]::Is64BitOperatingSystem) {
    Fail "Laintas CLI for Windows requires 64-bit Windows." $EXIT_UNSUPPORTED_HOST
}
if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {
    Fail "This package is for x86_64 Windows. Windows ARM64 packaging is not available yet." $EXIT_UNSUPPORTED_HOST
}

# wsl.exe exists on every current Windows whether or not the subsystem is
# actually enabled, so its presence proves nothing. Ask it for its status and
# fail early only on an HRESULT that names a definite cause -- an old WSL that
# does not know --status just returns an unrecognised error, which is not a
# reason to stop.
$status = Invoke-Wsl --status
if ($status.ExitCode -ne 0) {
    $diagnosis = Get-WslDiagnosis $status.Output
    if ($diagnosis) {
        Fail $diagnosis $EXIT_WSL
    }
}

$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Rootfs = Join-Path $PackageRoot "laintas-rootfs.tar.gz"
$LinuxBinary = Join-Path $PackageRoot "laintas-cli-linux"
$BundledLauncher = Join-Path $PackageRoot "laintas-cli.exe"
$BundledIcon = Join-Path $PackageRoot "icon.ico"
$BundledFragment = Join-Path $PackageRoot "terminal-fragment.json"
$BundledSettings = Join-Path $PackageRoot "terminal-settings.json"
$BundledTerminal = Join-Path $PackageRoot "terminal"
$TerminalDir = Join-Path $InstallRoot "terminal"
$BinDir = Join-Path $InstallRoot "bin"
$DistroDir = Join-Path $InstallRoot "WSL"
$InstalledLauncher = Join-Path $BinDir "laintas-cli.exe"

if (-not (Test-Path -LiteralPath $BundledLauncher -PathType Leaf)) {
    Fail "The Windows package is incomplete: laintas-cli.exe is missing. Download the installer again." $EXIT_PACKAGE_INCOMPLETE
}
if (-not (Test-Path -LiteralPath $LinuxBinary -PathType Leaf)) {
    Fail "The Windows installer is incomplete: the Linux runtime is missing. Download the installer again." $EXIT_PACKAGE_INCOMPLETE
}

$registered = Get-RegisteredDistributions
$importedNow = $false
$needsImport = $registered -notcontains $DistroName
$rebuilding = $false

if (-not $needsImport) {
    $registeredBase = Get-DistributionBasePath $DistroName
    if ($registeredBase) {
        $selectedBase = [IO.Path]::GetFullPath($DistroDir).TrimEnd('\')
        $registeredBase = [IO.Path]::GetFullPath($registeredBase).TrimEnd('\')
        if (-not $selectedBase.Equals($registeredBase, [StringComparison]::OrdinalIgnoreCase)) {
            Fail "$DistroName is already installed at $registeredBase. Keep its existing installation directory ($([IO.Path]::GetDirectoryName($registeredBase))) when upgrading." $EXIT_INSTALL_STATE
        }

        # A registered distribution whose virtual disk is gone. Unregistering
        # is the only way to clear the registration, and WSL will not do it
        # on its own: it keeps the entry, `wsl --list` keeps naming the
        # distribution, and every call into it fails. Deleting the multi-GB
        # ext4.vhdx by hand to reclaim disk space is the ordinary way to get
        # here, and before this the installer answered it by skipping the
        # import and then failing on the first command it ran inside the
        # distribution that no longer had a filesystem.
        $disk = Join-Path $registeredBase "ext4.vhdx"
        if (-not (Test-Path -LiteralPath $disk -PathType Leaf)) {
            Write-Host "$DistroName is registered but its virtual disk is missing; rebuilding it."
            $drop = Invoke-Wsl --unregister $DistroName
            if ($drop.ExitCode -ne 0) {
                $diagnosis = Get-WslDiagnosis $drop.Output
                if (-not $diagnosis) {
                    $diagnosis = "$DistroName is registered but its virtual disk ($disk) is gone, and the stale registration could not be removed (exit code $($drop.ExitCode)): $($drop.Output) Run 'wsl --unregister $DistroName' in PowerShell, then run this installer again."
                }
                Fail $diagnosis $EXIT_INSTALL_STATE
            }
            $needsImport = $true
            $rebuilding = $true
        }
    }
}

if ($needsImport) {
    if (-not (Test-Path -LiteralPath $Rootfs -PathType Leaf)) {
        Fail "The Windows package is incomplete: laintas-rootfs.tar.gz is missing. Download the installer again." $EXIT_PACKAGE_INCOMPLETE
    }
    $createdDistroDir = $false
    if (Test-Path -LiteralPath $DistroDir) {
        # Whatever is left in there when rebuilding is this distribution's own
        # debris: the relocation check above already established that WSL had
        # it registered at exactly this path. The guard is for a directory
        # that holds someone else's files.
        $existing = @()
        if (-not $rebuilding) {
            $existing = @(Get-ChildItem -LiteralPath $DistroDir -Force -ErrorAction SilentlyContinue)
        }
        if ($existing.Count -gt 0) {
            Fail "$DistroDir already contains files but no $DistroName distribution is registered, so importing there would overwrite them. This is what a hand-run 'wsl --unregister $DistroName' leaves behind: delete that directory, or choose another installation directory." $EXIT_INSTALL_STATE
        }
    } else {
        New-Item -ItemType Directory -Path $DistroDir -Force | Out-Null
        $createdDistroDir = $true
    }

    Write-Host "Importing the private $DistroName WSL environment..."
    try {
        $import = Invoke-Wsl --import $DistroName $DistroDir $Rootfs --version 2
        if ($import.Output) { Write-Host $import.Output }
        if ($import.ExitCode -ne 0) {
            $diagnosis = Get-WslDiagnosis $import.Output
            if (-not $diagnosis) {
                $diagnosis = "WSL could not create the private $DistroName distribution (exit code $($import.ExitCode)): $($import.Output) Run 'wsl --status' and 'wsl --update' in PowerShell to check that WSL 2 is working."
            }
            Fail $diagnosis $EXIT_WSL
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
    Fail "Cannot translate the installer runtime path for WSL: $fullLinuxBinary. Install from a local drive letter, not a UNC path." $EXIT_INSTALL_STATE
}
$drive = $Matches[1].ToLowerInvariant()
$relative = $Matches[2] -replace '\\', '/'
$wslLinuxBinary = "/mnt/$drive/$relative"
$runtime = Invoke-Wsl --distribution $DistroName --user root --exec /usr/bin/install -m 0755 $wslLinuxBinary /usr/local/bin/laintas-cli
if ($runtime.ExitCode -ne 0) {
    if ($importedNow) {
        & wsl.exe --unregister $DistroName 2>$null | Out-Null
    }
    $diagnosis = Get-WslDiagnosis $runtime.Output
    if (-not $diagnosis) {
        $diagnosis = "Could not install the Laintas runtime inside $DistroName (exit code $($runtime.ExitCode)): $($runtime.Output) If the distribution is registered but broken, run 'wsl --unregister $DistroName' in PowerShell and install again -- that rebuilds it from scratch."
    }
    Fail $diagnosis $EXIT_WSL
}

# Reclaim what the upgrade just cost. A WSL2 distribution lives in an
# ext4.vhdx that only ever grows: installing a ~55 MB runtime over the old
# one frees the old blocks inside the filesystem, but the virtual disk keeps
# every block it has ever touched, so each upgrade added its full size to the
# C: drive forever. fstrim hands the freed blocks back, and a sparse VHD lets
# Windows actually shrink the file. Both are best-effort — an older WSL has
# no --manage, and neither failing is a reason to fail an install.
try {
    & wsl.exe --distribution $DistroName --user root -- `
        /bin/sh -c 'command -v fstrim >/dev/null 2>&1 && fstrim -a || true' 2>$null | Out-Null
} catch {
    Write-Host "Could not trim unused blocks inside $DistroName (harmless)."
}
try {
    # --set-sparse needs the distribution stopped, and stopping it here is
    # free: nothing is running in it yet.
    & wsl.exe --terminate $DistroName 2>$null | Out-Null
    & wsl.exe --manage $DistroName --set-sparse true 2>$null | Out-Null
} catch {
    Write-Host "Could not make the $DistroName disk sparse (needs a newer WSL)."
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

# The bundled Windows Terminal, for machines that have none. conhost reports
# mouse input only as INPUT_RECORDs and never as VT sequences, so a WSL
# process running there cannot receive a click at all — on Windows 10 that
# leaves no terminal on the machine capable of running this CLI. Installed
# unconditionally so it is there if the user's own Terminal is later removed;
# the launcher prefers theirs whenever one exists.
$bundledTerminalInstalled = $false
if (Test-Path -LiteralPath $BundledTerminal -PathType Container) {
    try {
        if (Test-Path -LiteralPath $TerminalDir) {
            Remove-Item -LiteralPath $TerminalDir -Recurse -Force
        }
        New-Item -ItemType Directory -Path $TerminalDir -Force | Out-Null
        # Enumerate and copy rather than passing a wildcard: -LiteralPath does
        # not expand one, so `Join-Path $BundledTerminal "*"` asks for a file
        # actually named "*", throws, and leaves an empty terminal directory
        # behind — which is precisely what shipped in v1.23.6. -Force here is
        # for hidden entries, not for overwriting.
        Get-ChildItem -LiteralPath $BundledTerminal -Force |
            Copy-Item -Destination $TerminalDir -Recurse -Force
        # Portable mode keeps this copy's settings beside its executable, so
        # it never reads or writes the user's own Windows Terminal profile.
        $portableMarker = Join-Path $TerminalDir ".portable"
        if (-not (Test-Path -LiteralPath $portableMarker)) {
            New-Item -ItemType File -Path $portableMarker -Force | Out-Null
        }
        if (Test-Path -LiteralPath $BundledSettings -PathType Leaf) {
            $SettingsDir = Join-Path $TerminalDir "settings"
            New-Item -ItemType Directory -Path $SettingsDir -Force | Out-Null
            $settings = Get-Content -LiteralPath $BundledSettings -Raw -Encoding UTF8
            $settings = $settings.Replace("__LAUNCHER__", $InstalledLauncher.Replace("\", "\\"))
            $settings = $settings.Replace("__ICON__", $InstalledIcon.Replace("\", "\\"))
            [IO.File]::WriteAllText((Join-Path $SettingsDir "settings.json"),
                                    $settings,
                                    (New-Object Text.UTF8Encoding $false))
        }
        # Confirm the thing this exists to provide is actually on disk. The
        # copy above failed silently for a whole release because its only
        # report was a line in a scrolling installer log.
        $bundledExeCheck = Join-Path $TerminalDir "WindowsTerminal.exe"
        if (-not (Test-Path -LiteralPath $bundledExeCheck -PathType Leaf)) {
            throw "WindowsTerminal.exe is missing from $TerminalDir after copying."
        }
        $bundledTerminalInstalled = $true
    } catch {
        Write-Warning "Could not install the bundled terminal: $($_.Exception.Message)"
        # Leave nothing half-installed: an empty terminal directory looks like
        # a working one to anyone checking, including the launcher.
        if (Test-Path -LiteralPath $TerminalDir) {
            Remove-Item -LiteralPath $TerminalDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

$wt = Get-Command wt.exe -ErrorAction SilentlyContinue

$Programs = [Environment]::GetFolderPath("Programs")
if ($Programs) {
    $ShortcutPath = Join-Path $Programs "Laintas CLI.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    # Point the shortcut at Windows Terminal when it is available, so the
    # profile's font, colours and mouse handling apply from the first launch.
    # Starting the launcher directly gets whatever terminal Windows defaults
    # to, which on Windows 10 is the legacy console.
    $bundledExe = Join-Path $TerminalDir "WindowsTerminal.exe"
    if ($terminalProfile -and $wt) {
        $Shortcut.TargetPath = $wt.Source
        $Shortcut.Arguments = '-p "Laintas CLI"'
    } elseif ($bundledTerminalInstalled -and (Test-Path -LiteralPath $bundledExe -PathType Leaf)) {
        # Its portable settings name our profile as the default, so it needs
        # no arguments. Pointing the shortcut straight at it keeps the very
        # first launch out of conhost rather than relying on the launcher to
        # escalate out of a console the user has already seen.
        $Shortcut.TargetPath = $bundledExe
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
}
if ($bundledTerminalInstalled) {
    Write-Host "Bundled terminal: $TerminalDir (used only if you have no Windows Terminal)"
} elseif (-not $wt) {
    Write-Host "No terminal capable of mouse input was found. Install Windows Terminal for the full interface."
}
if ($pathChanged) {
    Write-Host "Open a new terminal, then run: laintas-cli"
} else {
    Write-Host "Run: laintas-cli"
}
try { Stop-Transcript | Out-Null } catch { }
