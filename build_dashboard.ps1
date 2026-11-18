param([string]$Python = 'python', [switch]$UseApprovedTestException)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $tests = @('test_large_data', 'test_shared_dashboard', 'test_named_sensor_data', 'test_aqua_connector', 'test_category_reports', 'test_powerpoint_export', 'test_shared_powerpoint')
    if ($UseApprovedTestException) {
        Write-Warning 'Approved exception: legacy-loader and AQUA integration failures remain unverified. See PACKAGE_README.md.'
        $tests = @('test_shared_dashboard', 'test_named_sensor_data', 'test_category_reports', 'test_powerpoint_export', 'test_shared_powerpoint')
    }
    & $Python -m unittest @tests -q
    if ($LASTEXITCODE -ne 0) { throw 'Dashboard tests failed.' }
    & $Python run_dashboard.py --publish-profiles --publish-folder output\shared
    if ($LASTEXITCODE -ne 0) { throw 'Static HTML publication failed.' }
    $index = Join-Path $PSScriptRoot 'output\shared\index.html'
    if (-not (Test-Path $index)) { throw 'Static HTML index was not generated.' }
    Write-Host "Static dashboard ready: $index"
} finally {
    Pop-Location
}