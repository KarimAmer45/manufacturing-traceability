param(
    [ValidateSet('migrate', 'demo', 'test', 'export', 'simulate', 'benchmark', 'backup')]
    [string]$Action = 'demo',
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (Test-Path -LiteralPath "$projectRoot\.env") {
    foreach ($line in Get-Content -LiteralPath "$projectRoot\.env") {
        if ($line -match '^([A-Z_]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
}
$localDeps = Join-Path $projectRoot '.local\pydeps'
if (Test-Path -LiteralPath $localDeps) { $env:PYTHONPATH = "$localDeps;$env:PYTHONPATH" }
$commands = @{
    migrate = @('scripts/migrate.py')
    demo = @('scripts/demo.py')
    test = @('-m', 'pytest', '-q', 'tests')
    export = @('scripts/export_results.py')
    simulate = @('ingest/p1_simulate_plant.py')
    benchmark = @('scripts/benchmark_bosch.py')
    backup = @('scripts/backup_verify.py')
}
Push-Location $projectRoot
try {
    & $Python @($commands[$Action])
    if ($LASTEXITCODE -ne 0) { throw "Project command failed with exit code $LASTEXITCODE" }
} finally { Pop-Location }
