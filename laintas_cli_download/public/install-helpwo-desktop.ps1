# Download the current official Windows installer and verify it before launch.
& {
    $ErrorActionPreference = 'Stop'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $release = Invoke-RestMethod -Uri 'https://cli.laintas.com/windows/download.json' -ErrorAction Stop
    $version = [string]$release.version
    $checksum = [string]$release.sha256
    $size = 0L
    if ($version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$' -or
        [string]$release.installer -cne "https://cli.laintas.com/windows/releases/$version/LaintasSetup.exe" -or
        $checksum -notmatch '^[0-9a-fA-F]{64}$' -or
        -not [long]::TryParse([string]$release.size_bytes, [ref]$size) -or $size -le 0) {
        throw 'The Windows release metadata is invalid. No installer was run.'
    }
    $directory = Join-Path ([IO.Path]::GetTempPath()) ('laintas-install-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $directory -ErrorAction Stop | Out-Null
    try {
        $installer = Join-Path $directory 'LaintasSetup.exe'
        Write-Host "Downloading Laintas Windows v$version..."
        Invoke-WebRequest -Uri $release.installer -OutFile $installer -UseBasicParsing -ErrorAction Stop
        if ((Get-Item -LiteralPath $installer).Length -ne $size -or
            (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -ine $checksum) {
            throw 'The installer failed its size or SHA-256 check. No installer was run.'
        }
        Write-Host 'Installer verified. Opening the installation wizard...'
        $process = Start-Process -FilePath $installer -Wait -PassThru -ErrorAction Stop
        if ($process.ExitCode -ne 0) { throw "The installer exited with code $($process.ExitCode)." }
        Write-Host 'Installation completed. Start Laintas Windows from the Start menu.'
    } finally {
        Remove-Item -LiteralPath $directory -Recurse -Force -ErrorAction SilentlyContinue
    }
}
