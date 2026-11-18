param([string]$Destination)
$ErrorActionPreference = 'Stop'
$release = Get-Content (Join-Path $PSScriptRoot 'release.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Destination) { $Destination = $release.distribution_root }
$source = Join-Path $PSScriptRoot 'output\shared'
if (-not (Test-Path (Join-Path $source 'index.html'))) {
    throw 'Run build_dashboard.ps1 first to generate output/shared/index.html.'
}
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
foreach ($folder in @('reports', 'exports')) {
    $from = Join-Path $source $folder
    if (Test-Path $from) {
        $to = Join-Path $Destination $folder
        New-Item -ItemType Directory -Path $to -Force | Out-Null
        Get-ChildItem -LiteralPath $from | Copy-Item -Destination $to -Recurse -Force
    }
}
$temporary = Join-Path $Destination ".index-$([guid]::NewGuid().ToString('N')).tmp"
Copy-Item (Join-Path $source 'index.html') $temporary
$index = Join-Path $Destination 'index.html'
try {
    if (Test-Path $index) {
        $backup = Join-Path $Destination ".index-backup-$([guid]::NewGuid().ToString('N')).tmp"
        [System.IO.File]::Replace($temporary, $index, $backup)
        Remove-Item -LiteralPath $backup -Force
    } else {
        [System.IO.File]::Move($temporary, $index)
    }
} finally {
    if (Test-Path $temporary) { Remove-Item -LiteralPath $temporary -Force }
}
Copy-Item (Join-Path $PSScriptRoot 'START_THERMAL_DASHBOARD.bat') $Destination -Force
Copy-Item (Join-Path $PSScriptRoot 'PACKAGE_README.md') (Join-Path $Destination 'README.md') -Force
Copy-Item (Join-Path $PSScriptRoot 'publish_dashboard.ps1') $Destination -Force
Write-Host "Published HTML: $index"
Write-Host "Open: $(Join-Path $Destination 'START_THERMAL_DASHBOARD.bat')"