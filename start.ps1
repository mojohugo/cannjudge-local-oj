param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'var\remote-deployment.json')) {
    $migration = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'var\remote-deployment.json') -Raw | ConvertFrom-Json
    Write-Host "OJ and MCP run on the configured remote server: $($migration.public_url)"
    Start-Process $migration.public_url
    return
}
$env:OJ_LOCAL_ORIGIN = "http://127.0.0.1:$Port"
python -c "from oj.config import load_settings; load_settings(); print('Credentials: var/admin-access.txt')"
python -m uvicorn oj.app:create_app --factory --host 127.0.0.1 --port $Port --workers 1 --no-proxy-headers
