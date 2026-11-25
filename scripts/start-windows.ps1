param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

function New-SichasKey {
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $bytes = New-Object byte[] 32
        $random.GetBytes($bytes)
        [Convert]::ToBase64String($bytes)
    } finally {
        $random.Dispose()
    }
}

if (-not (Test-Path -LiteralPath '.env')) {
    @("SECRET_KEY=$(New-SichasKey)", "SELFIE_ENCRYPTION_KEY=$(New-SichasKey)") |
        Set-Content -LiteralPath '.env' -Encoding ascii
}
docker info --format '{{.OSType}}' 2>$null | ForEach-Object {
    if ($_ -ne 'linux') { throw 'Enable Linux containers in Docker Desktop.' }
}
if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop and retry.' }
docker compose up --build -d --wait
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose failed to start.' }
$health = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/health'
if (-not $health.ok) { throw 'Application health check failed.' }
Write-Host "Sichas $($health.version) is ready at http://127.0.0.1:8080/"
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8080/' }
