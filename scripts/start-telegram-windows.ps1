param(
    [Parameter(Mandatory=$true)][Uri]$PublicUrl,
    [Parameter(Mandatory=$true)][ValidatePattern('^[1-9][0-9]*$')][string]$AdminTelegramId
)
$ErrorActionPreference = 'Stop'
if ($PublicUrl.Scheme -ne 'https' -or !$PublicUrl.Host -or $PublicUrl.AbsolutePath -ne '/' -or
    $PublicUrl.UserInfo -or $PublicUrl.Query -or $PublicUrl.Fragment) {
    throw 'Use the HTTPS origin, without a path, query or credentials.'
}

function Invoke-Compose {
    & docker compose --project-name sichas-telegram --env-file .env.pc-telegram -f compose.production.yaml @args
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose failed. See the output above.' }
}

function New-Key {
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    return [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
}

$origin = $PublicUrl.GetLeftPart([UriPartial]::Authority)
$previousEnvFile = $env:SICHAS_ENV_FILE
$previousPort = $env:SICHAS_HTTP_PORT
Push-Location (Join-Path $PSScriptRoot '..')
try {
    $env:SICHAS_ENV_FILE = '.env.pc-telegram'
    $env:SICHAS_HTTP_PORT = '8082'
    $config = '.env.pc-telegram'
    if (!(Test-Path -LiteralPath $config)) {
        $secret = Read-Host -Prompt 'Telegram bot token' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
        try {
            $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
            if ($token -notmatch '^[0-9]+:[A-Za-z0-9_-]+$') { throw 'Invalid Telegram bot token format.' }
            $password = New-Key
            @("SECRET_KEY=$(New-Key)", "SELFIE_ENCRYPTION_KEY=$(New-Key)",
              "TELEGRAM_BOT_TOKEN=$token", "POSTGRES_PASSWORD=$password",
              "DATABASE_URL=postgresql://sichas:${password}@postgres:5432/sichas",
              "PUBLIC_URL=$origin", "ADMIN_TELEGRAM_IDS=$AdminTelegramId",
              'BOT_USERNAME=vmeste_rjadom_bot', 'ALLOW_TEST_AUTH=false', 'ENABLE_SSE=false') |
                Set-Content -LiteralPath $config -Encoding ascii
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
            Remove-Variable token -ErrorAction SilentlyContinue
        }
    } else {
        $existing = Get-Content -LiteralPath $config
        if ($existing -notcontains "PUBLIC_URL=$origin" -or $existing -notcontains "ADMIN_TELEGRAM_IDS=$AdminTelegramId") {
            throw 'The saved HTTPS origin or admin differs. Keep the saved values until the webhook and configuration have been reviewed.'
        }
    }

    Invoke-Compose build app
    $preflight = @'
import json
import os
from urllib.request import urlopen

token = os.environ['TELEGRAM_BOT_TOKEN']
def result(method):
    try:
        with urlopen('https://api.telegram.org/bot' + token + '/' + method, timeout=10) as response:
            body = json.load(response)
        if body.get('ok'):
            return body['result']
    except (OSError, ValueError, KeyError):
        pass
    raise SystemExit('Telegram preflight failed. Check the token and network connection.')

if result('getMe').get('username', '').casefold() != os.environ['BOT_USERNAME'].casefold():
    raise SystemExit('The token belongs to a different bot.')
current = result('getWebhookInfo').get('url')
expected = os.environ['PUBLIC_URL'].rstrip('/') + '/telegram/webhook'
if current and current != expected:
    raise SystemExit('A different Telegram webhook is active. Review it before switching.')
print('Telegram bot and webhook preflight passed.')
'@
    Invoke-Compose run --rm --no-deps -T app python -c $preflight
    Invoke-Compose up -d --wait postgres
    $count = Invoke-Compose exec -T postgres psql -U sichas -d sichas -Atqc "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
    if ($count -eq '0') {
        Invoke-Compose run --rm --no-deps -T app python bootstrap_db.py
    } else {
        Invoke-Compose run --rm --no-deps -T app alembic upgrade head
    }
    Invoke-Compose up -d --wait app
    $ready = Invoke-RestMethod -Uri "$origin/health" -TimeoutSec 15
    for ($attempt = 0; $ready.ok -and !$ready.telegram_bot_configured -and $attempt -lt 12; $attempt++) {
        Start-Sleep -Seconds 5
        $ready = Invoke-RestMethod -Uri "$origin/health" -TimeoutSec 15
    }
    if (!$ready.ok -or !$ready.telegram_bot_configured) { throw 'HTTPS or Telegram bot setup is not ready.' }
    if (!(Invoke-RestMethod -Uri "$origin/ready" -TimeoutSec 15).ok) { throw 'Application readiness check failed.' }
    Write-Host "Sichas is reachable at $origin and ready for Telegram testing."
} finally {
    $env:SICHAS_ENV_FILE = $previousEnvFile
    $env:SICHAS_HTTP_PORT = $previousPort
    Pop-Location
}
