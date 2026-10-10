# Download the current official Windows installer and verify it before launch.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'

    # ASCII source stays readable in Windows PowerShell 5.1's legacy encoding.
    # The displayed rail matches download_progress.py and install.sh.
    function Format-LaintasSize([double]$Bytes) {
        $units = @('B', 'KB', 'MB', 'GB'); $unit = 0
        while ($Bytes -ge 1024 -and $unit -lt 3) { $Bytes /= 1024; $unit++ }
        $format = if ($unit -eq 0) { '0' } else { '0.0' }
        return $Bytes.ToString($format, [Globalization.CultureInfo]::InvariantCulture) + ' ' + $units[$unit]
    }
    function Format-LaintasTime([double]$Seconds) {
        $value = [long][Math]::Floor([Math]::Max(0, $Seconds))
        return ('{0:00}:{1:00}' -f [Math]::Floor($value / 60), ($value % 60))
    }
    $script:LaintasColors = @{
        activity = @('63;185;80', 'Green'); muted = @('139;148;158', 'DarkGray')
        track = @('72;79;88', 'DarkGray'); error = @('248;81;73', 'Red')
        foreground = @('201;209;217', 'Gray'); strong = @('230;237;243', 'White')
    }
    $script:LaintasAnsi = [bool]($Host.UI.SupportsVirtualTerminal -or $env:WT_SESSION -or
        $env:TERM -match '^(xterm|screen|tmux)' -or $env:ConEmuANSI -eq 'ON')
    function Write-LaintasText([string]$Text, [string]$Role, [bool]$Plain) {
        if ($Plain) { Write-Host -NoNewline $Text }
        elseif ($script:LaintasAnsi) {
            $esc = [string][char]27
            $prefix = if ($Role -eq 'strong') { '[1;38;2;' } else { '[38;2;' }
            Write-Host -NoNewline ($esc + $prefix + $script:LaintasColors[$Role][0] + 'm' + $Text + $esc + '[0m')
        } else {
            Write-Host -NoNewline $Text -ForegroundColor ([ConsoleColor]$script:LaintasColors[$Role][1])
        }
    }
    function Test-LaintasPlain {
        return [bool]((Test-Path Env:NO_COLOR) -or $env:TERM -eq 'dumb' -or
                      [Console]::IsOutputRedirected)
    }
    function Show-LaintasProgress($State, [string]$Outcome = '') {
        if (-not $State.Animate -and -not $Outcome) { return }
        $elapsed = $State.Timer.Elapsed.TotalSeconds
        if (-not $Outcome -and $elapsed - $State.LastPaint -lt 0.15) { return }
        $State.LastPaint = $elapsed
        if ($State.Animate) {
            # Spaces clear the entire old row; reserve the final terminal cell.
            Write-Host -NoNewline ("`r" + (' ' * ($State.Width - 1)) + "`r")
        }
        if ($Outcome) {
            $mark = if ($Outcome -eq 'success') { [char]0x2713 } else { [char]0x2717 }
            $word = if ($Outcome -eq 'success') { '100%' } else { 'Failed' }
            $role = if ($Outcome -eq 'success') { 'activity' } else { 'error' }
            Write-LaintasText "  $mark " $role $State.Plain
            $wordRole = if ($Outcome -eq 'success') { 'foreground' } else { 'error' }
            Write-LaintasText $word $wordRole $State.Plain
            $summary = '  ' + (Format-LaintasSize $State.Bytes) + ' ' + [char]0x00b7 + ' ' + (Format-LaintasTime $elapsed)
            if ($summary.Length + 8 -gt $State.Width) { $summary = '' }
            Write-LaintasText $summary muted $State.Plain
            Write-Host ''
            return
        }
        $speed = if ($elapsed -gt 0) { $State.Bytes / $elapsed } else { 0 }
        $percent = '  --%'
        $amount = Format-LaintasSize $State.Bytes
        if ($State.Total -gt 0) {
            $percent = '{0,3}%' -f [Math]::Min(99, [Math]::Floor($State.Bytes * 100.0 / $State.Total))
            $divisor = 1.0; $unit = 'B'
            foreach ($nextUnit in @('KB', 'MB', 'GB')) {
                if ($State.Total -lt $divisor * 1024) { break }
                $divisor *= 1024; $unit = $nextUnit
            }
            $format = if ($divisor -eq 1) { '0' } else { '0.0' }
            $amount = ($State.Bytes / $divisor).ToString($format, [Globalization.CultureInfo]::InvariantCulture) + '/' +
                ($State.Total / $divisor).ToString($format, [Globalization.CultureInfo]::InvariantCulture) + ' ' + $unit
        }
        $fields = if ($State.Total -gt 0) { @($percent, $amount, ((Format-LaintasSize $speed) + '/s')) }
                  else { @($amount, ((Format-LaintasSize $speed) + '/s')) }
        $eta = if ($State.Total -gt 0 -and $speed -gt 0) { Format-LaintasTime (($State.Total - $State.Bytes) / $speed) } else { '--:--' }
        if ($State.Total -gt 0) { $fields += $eta }
        while ($fields.Count -gt 2 -and 12 + ($fields -join '  ').Length -gt $State.Width - 1) {
            $fields = $fields[0..($fields.Count - 2)]
        }
        $metrics = $fields -join '  '
        $railWidth = [int][Math]::Max(1, [Math]::Min(32, $State.Width - 5 - $metrics.Length))
        $rail = ''
        if ($State.Total -gt 0) {
            $filled = [int][Math]::Min($railWidth, [Math]::Floor($State.Bytes * $railWidth / $State.Total))
            $rail = ([string][char]0x2501) * $filled
            Write-LaintasText ('  ' + $rail) activity $State.Plain
            if ($filled -lt $railWidth) {
                Write-LaintasText ([string][char]0x2578) activity $State.Plain
                Write-LaintasText (([string][char]0x2500) * ($railWidth - $filled - 1)) track $State.Plain
            }
        } else {
            $pulse = [int][Math]::Min(4, $railWidth)
            $span = $railWidth - $pulse
            $tick = if ($span -gt 0) { [int][Math]::Floor($elapsed / 0.15) % (2 * $span) } else { 0 }
            $position = [int][Math]::Min($tick, 2 * $span - $tick)
            Write-LaintasText ('  ' + (([string][char]0x2500) * $position)) track $State.Plain
            Write-LaintasText (([string][char]0x2501) * $pulse) activity $State.Plain
            Write-LaintasText (([string][char]0x2500) * ($railWidth - $pulse - $position)) track $State.Plain
        }
        $row = '  ' + $metrics
        $space = [Math]::Max(0, $State.Width - 3 - $railWidth)
        if ($row.Length -gt $space) { $row = $row.Substring(0, $space) }
        $headLength = if ($State.Total -gt 0) { [Math]::Min($row.Length, 2 + $percent.Length) } else { 0 }
        Write-LaintasText $row.Substring(0, $headLength) strong $State.Plain
        if ($row.Length -gt $headLength) {
            Write-LaintasText $row.Substring($headLength) muted $State.Plain
        }
    }
    function Get-LaintasDownload([string]$Url, [string]$Path, [long]$ExpectedSize) {
        Add-Type -AssemblyName System.Net.Http
        $width = 80; $animate = $false
        try {
            $animate = -not [Console]::IsOutputRedirected -and $env:TERM -ne 'dumb'
            if ($animate -and [Console]::WindowWidth -gt 0) { $width = [Console]::WindowWidth }
        } catch { $animate = $false }
        $state = [PSCustomObject]@{
            Bytes = 0L; Total = $ExpectedSize; Width = [Math]::Max(10, $width)
            Animate = $animate; Plain = [bool](Test-LaintasPlain)
            Timer = [Diagnostics.Stopwatch]::StartNew(); LastPaint = -1.0
        }
        Write-LaintasText ('  ' + [char]0x2193 + ' ') activity $state.Plain
        Write-LaintasText 'LaintasSetup.exe' foreground $state.Plain
        Write-Host ''
        $client = New-Object Net.Http.HttpClient
        $cancel = New-Object Threading.CancellationTokenSource
        $cancel.CancelAfter(900000)
        $response = $null; $inputStream = $null; $output = $null
        try {
            # Headers first: keep the large installer out of the HTTP buffer.
            $task = $client.GetAsync($Url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token)
            while (-not $task.Wait(150)) {
                $cancel.Token.ThrowIfCancellationRequested()
                Show-LaintasProgress $state
            }
            $response = $task.GetAwaiter().GetResult()
            $null = $response.EnsureSuccessStatusCode()
            $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $output = [IO.File]::Create($Path)
            $buffer = New-Object byte[] 65536
            Show-LaintasProgress $state
            while ($true) {
                $read = $inputStream.ReadAsync($buffer, 0, $buffer.Length, $cancel.Token)
                while (-not $read.Wait(150)) {
                    $cancel.Token.ThrowIfCancellationRequested()
                    Show-LaintasProgress $state
                }
                $count = $read.GetAwaiter().GetResult()
                if ($count -eq 0) { break }
                $output.Write($buffer, 0, $count)
                $state.Bytes += $count
                Show-LaintasProgress $state
            }
            $output.Dispose(); $output = $null
            if ($state.Bytes -ne $ExpectedSize) { throw 'The installer download is incomplete.' }
            Show-LaintasProgress $state 'success'
        } catch {
            Show-LaintasProgress $state 'failed'
            throw
        } finally {
            $cancel.Cancel()
            if ($output) { $output.Dispose() }
            if ($inputStream) { $inputStream.Dispose() }
            if ($response) { $response.Dispose() }
            $client.Dispose(); $cancel.Dispose(); $state.Timer.Stop()
        }
    }
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
        Write-Host "Laintas Windows v$version"
        Get-LaintasDownload $release.installer $installer $size
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
