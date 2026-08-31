[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$DistroName = "Laintas-CLI",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Laintas"),
    [switch]$DeleteLinuxData
)

$ErrorActionPreference = "Stop"
$BinDir = Join-Path $InstallRoot "bin"
$Launcher = Join-Path $BinDir "laintas-cli.exe"
$Icon = Join-Path $BinDir "laintas-cli.ico"

if ($DeleteLinuxData) {
    if ($PSCmdlet.ShouldProcess($DistroName, "Unregister and permanently delete its Linux filesystem")) {
        & wsl.exe --unregister $DistroName
        if ($LASTEXITCODE -ne 0) {
            throw "Could not unregister $DistroName (exit code $LASTEXITCODE)."
        }
    }
} else {
    Write-Host "Keeping the $DistroName Linux filesystem and ~/.laintas data."
}

if (Test-Path -LiteralPath $Launcher) {
    Remove-Item -LiteralPath $Launcher -Force
}
if (Test-Path -LiteralPath $Icon) {
    Remove-Item -LiteralPath $Icon -Force
}

# The Windows Terminal profile points at a launcher that is about to stop
# existing. Left behind it would keep offering itself in the dropdown and
# fail when picked. Only this application's own fragment directory is
# removed, never the shared Fragments/ directory above it.
$FragmentDir = Join-Path $env:LOCALAPPDATA "Microsoft\Windows Terminal\Fragments\Laintas.LaintasCLI"
if (Test-Path -LiteralPath $FragmentDir) {
    Remove-Item -LiteralPath $FragmentDir -Recurse -Force -ErrorAction SilentlyContinue
}

$current = [Environment]::GetEnvironmentVariable("Path", "User")
$updated = @($current -split ";" | Where-Object { $_ -and $_ -ne $BinDir }) -join ";"
[Environment]::SetEnvironmentVariable("Path", $updated, "User")

$Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Laintas CLI.lnk"
if (Test-Path -LiteralPath $Shortcut) {
    Remove-Item -LiteralPath $Shortcut -Force
}

Write-Host "Laintas CLI launcher removed."
