param([ValidateSet('start','stop','status')][string]$Action = 'status')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pgCtl = Join-Path $projectRoot '.local\postgres\pgsql\bin\pg_ctl.exe'
$pgData = Join-Path $projectRoot '.local\pgdata'
if (-not (Test-Path -LiteralPath $pgCtl) -or -not (Test-Path -LiteralPath $pgData)) {
    throw 'This optional portable runtime is not present. Use Docker or an installed PostgreSQL server.'
}
switch ($Action) {
    start { & $pgCtl -D $pgData -l "$projectRoot\.local\postgres.log" -o '-h 127.0.0.1 -p 55433' -w start }
    stop { & $pgCtl -D $pgData -m fast -w stop }
    status { & $pgCtl -D $pgData status }
}
exit $LASTEXITCODE
