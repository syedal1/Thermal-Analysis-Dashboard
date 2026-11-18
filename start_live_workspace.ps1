$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$url = 'http://127.0.0.1:8063/'

function Get-WorkspaceStatus {
    try {
        $response = Invoke-RestMethod -Uri ($url + 'api/workspace-info') -TimeoutSec 2
        if ($response.application -eq 'Thermal Analysis Dashboard' -and
            $response.workspace -and
            [System.IO.Path]::GetFullPath($response.workspace).TrimEnd('\') -eq $root) {
            return 'ready'
        }
        return 'occupied'
    }
    catch {
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $client.Connect('127.0.0.1', 8063)
            return 'occupied'
        }
        catch { return 'available' }
        finally { $client.Dispose() }
    }
}

try {
    $status = Get-WorkspaceStatus
    if ($status -eq 'occupied') {
        throw 'Port 8063 is in use by another service or an older dashboard. Close it before starting this workspace.'
    }
    if ($status -ne 'ready') {
        $python = Join-Path $root '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
            $command = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $command) { throw 'Python was not found. Install Python and the dashboard requirements, then try again.' }
            $python = $command.Source
        }
        Write-Host 'Starting the live Thermal Analysis workspace...'
        $server = Start-Process -FilePath $python -WorkingDirectory $root -PassThru -ArgumentList @(
            '"' + (Join-Path $root 'run_dashboard.py') + '"',
            '--profile-portal', '--port', '8063', '--service-url', $url.TrimEnd('/')
        )
        $ready = $false
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            if ((Get-WorkspaceStatus) -eq 'ready') { $ready = $true; break }
            $server.Refresh()
            if ($server.HasExited) { break }
            [void][System.Threading.ManualResetEvent]::new($false).WaitOne(500)
        }
        if (-not $ready) {
            throw 'The dashboard did not start. Check the server window, Python dependencies, and port 8063, then try again.'
        }
    }
    Write-Host "Opening $url"
    Write-Host 'Keep the dashboard server window open while processing reports.'
    Start-Process $url
}
catch {
    Write-Host "Unable to open the live workspace: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}