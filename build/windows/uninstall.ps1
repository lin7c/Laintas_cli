[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$DistroName = "Laintas-CLI",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Laintas"),
    [switch]$DeleteLinuxData
)

$ErrorActionPreference = "Stop"
$BinDir = Join-Path $InstallRoot "bin"
$Launcher = Join-Path $BinDir "laintas-cli.exe"

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

$current = [Environment]::GetEnvironmentVariable("Path", "User")
$updated = @($current -split ";" | Where-Object { $_ -and $_ -ne $BinDir }) -join ";"
[Environment]::SetEnvironmentVariable("Path", $updated, "User")

$Shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Laintas CLI.lnk"
if (Test-Path -LiteralPath $Shortcut) {
    Remove-Item -LiteralPath $Shortcut -Force
}

Write-Host "Laintas CLI launcher removed."
