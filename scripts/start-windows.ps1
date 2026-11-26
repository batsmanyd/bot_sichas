param([switch]$NoBrowser, [switch]$Demo, [switch]$Rollback)

$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw 'Docker command failed. See the output above.' }
}

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

function Open-Sichas($address, $path) {
    $health = Invoke-RestMethod -Uri "$address/health"
    if (-not $health.ok) { throw 'Application health check failed.' }
    Write-Host "Sichas $($health.version) is ready at $address$path"
    if (-not $NoBrowser) { Start-Process "$address$path" }
}

if ((Invoke-Docker info --format '{{.OSType}}') -ne 'linux') {
    throw 'Start Docker Desktop with Linux containers.'
}
if ($Demo) {
    if ($Rollback) { throw 'Choose -Demo or -Rollback.' }
    Invoke-Docker compose -f compose.demo.yaml up --build -d --wait
    Open-Sichas 'http://127.0.0.1:8081' '/demo'
    exit
}

$backupRoot = Join-Path (Get-Location) 'local-backups'
if (-not $Rollback -and -not (Test-Path -LiteralPath '.env')) {
    @("SECRET_KEY=$(New-SichasKey)", "SELFIE_ENCRYPTION_KEY=$(New-SichasKey)") |
        Set-Content -LiteralPath '.env' -Encoding ascii
}

$restoreCode = @'
from pathlib import Path
import os
import shutil
import sqlite3

source = Path('/backup/sichas.db')
with sqlite3.connect('file:/backup/sichas.db?mode=ro', uri=True) as db:
    if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
        raise RuntimeError('Backup integrity check failed')
target = Path('/data/sichas.db')
temporary = target.with_suffix('.restore')
shutil.copyfile(source, temporary)
os.chown(temporary, 10001, 10001)
temporary.chmod(0o600)
os.replace(temporary, target)
for suffix in ('-wal', '-shm'):
    Path(str(target) + suffix).unlink(missing_ok=True)
'@
$backupCode = @'
import sqlite3

with sqlite3.connect('file:/data/sichas.db?mode=ro', uri=True) as source:
    with sqlite3.connect('/backup/sichas.db') as snapshot:
        source.backup(snapshot)
        if snapshot.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise RuntimeError('Backup integrity check failed')
'@

$previousImage = $env:SICHAS_LOCAL_IMAGE
try {
    if ($Rollback) {
        $backup = Get-ChildItem -LiteralPath $backupRoot -Directory |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'image.txt') } |
            Sort-Object Name -Descending | Select-Object -First 1
        if (-not $backup) { throw 'No complete backup found.' }
        $env:SICHAS_LOCAL_IMAGE = (Get-Content -LiteralPath (Join-Path $backup.FullName 'image.txt')).Trim()
        Invoke-Docker image inspect $env:SICHAS_LOCAL_IMAGE --format '{{.Id}}' | Out-Null
        $savedEnv = Join-Path $backup.FullName '.env'
        if (-not (Test-Path -LiteralPath $savedEnv)) { throw 'Backup .env is missing.' }
        Invoke-Docker compose --env-file $savedEnv stop app
        Invoke-Docker compose --env-file $savedEnv run --no-deps --rm --user 0 -v "$($backup.FullName):/backup:ro" --entrypoint python app -c $restoreCode
        Copy-Item -LiteralPath $savedEnv -Destination '.env' -Force
        Invoke-Docker compose up --no-build -d --wait
    } else {
        $existing = Invoke-Docker compose ps --all --quiet app
        if ($existing) {
            $stamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fff')
            $snapshot = New-Item -ItemType Directory -Path (Join-Path $backupRoot $stamp) -Force
            $image = Invoke-Docker inspect $existing --format '{{.Image}}'
            $rollbackImage = "sichas-local:rollback-$stamp"
            Invoke-Docker tag $image $rollbackImage
            Copy-Item -LiteralPath '.env' -Destination (Join-Path $snapshot.FullName '.env')
            $env:SICHAS_LOCAL_IMAGE = $rollbackImage
            Invoke-Docker compose run --no-deps --rm --user 0 -v "$($snapshot.FullName):/backup" --entrypoint python app -c $backupCode
            Set-Content -LiteralPath (Join-Path $snapshot.FullName 'image.txt') -Encoding ascii -Value $rollbackImage
            Write-Host "Backup saved to $($snapshot.FullName)"
        }
        $env:SICHAS_LOCAL_IMAGE = 'sichas-local:current'
        Invoke-Docker compose up --build -d --wait
    }
    Open-Sichas 'http://127.0.0.1:8080' '/'
} finally {
    $env:SICHAS_LOCAL_IMAGE = $previousImage
}
