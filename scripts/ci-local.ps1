<#
.SYNOPSIS
  Runs the gates of .github/workflows/ci.yml on this machine: one PASS, FAIL or SKIP line per gate, exit code 1 if any
  gate failed.

.DESCRIPTION
  Databases: the test suite uses app_test on the local Postgres container (tests/conftest.py refuses any database whose
  name does not end in _test). The migration check and the eval gate each get a scratch database, also ending in _test,
  created and dropped here. The database `app` is never connected to.

  A gate is SKIP only when what it checks is not there: web gates before the web app exists, the secret scan when
  gitleaks is not installed (CI always runs it).

  Anything that downloads (uv sync, pnpm install) works only where the network works; run this from PowerShell.
#>
param(
    [string]$DbContainer = 'openelip-db-1',
    [string]$DbUser = 'app',
    [string]$DbPassword = 'app_local_only',
    [string]$DbHost = '127.0.0.1',
    [int]$DbPort = 5433
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repo 'backend'
$web = Join-Path $repo 'web'
Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue  # a stray environment from another project only produces warnings

$results = [System.Collections.Generic.List[string]]::new()
$failed = 0

function Step([string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe $($arguments -join ' ') exited with $LASTEXITCODE" }
}

function Gate([string]$name, [scriptblock]$body) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    try {
        & $body
        $line = "PASS $name"
    } catch [System.Management.Automation.RuntimeException] {
        if ($_.Exception.Message -like 'SKIP:*') {
            $line = "SKIP $name ($($_.Exception.Message.Substring(5).Trim()))"
        } else {
            $script:failed++
            Write-Host $_.Exception.Message -ForegroundColor Red
            $line = "FAIL $name"
        }
    }
    $results.Add($line)
}

function Skip([string]$why) { throw "SKIP: $why" }

# ---- scratch databases (always named *_test) -----------------------------------------------------------------------
function Psql([string]$sql) {
    & docker exec -e "PGPASSWORD=$DbPassword" $DbContainer psql -U $DbUser -d postgres -v ON_ERROR_STOP=1 -q -c $sql
    if ($LASTEXITCODE -ne 0) { throw "psql in $DbContainer failed: $sql" }
}

function Assert-TestName([string]$name) {
    if ($name -notmatch '^[a-z0-9_]+_test$') { throw "refusing database '$name': a scratch database name must end in _test" }
}

function New-ScratchDatabase([string]$name) {
    Assert-TestName $name
    Psql "DROP DATABASE IF EXISTS $name WITH (FORCE)"
    Psql "CREATE DATABASE $name"
}

function Remove-ScratchDatabase([string]$name) {
    Assert-TestName $name
    Psql "DROP DATABASE IF EXISTS $name WITH (FORCE)"
}

function Url([string]$role, [string]$password, [string]$database) {
    Assert-TestName $database
    "postgresql://${role}:${password}@${DbHost}:${DbPort}/${database}"
}

function In-Backend([scriptblock]$body) {
    Push-Location $backend
    try { & $body } finally { Pop-Location }
}

# ---- backend -------------------------------------------------------------------------------------------------------
Gate 'backend lint (ruff check)' { In-Backend { Step uv @('run', '--frozen', 'ruff', 'check', 'app', 'tests', '../db') } }
Gate 'backend formatting (ruff format --check)' { In-Backend { Step uv @('run', '--frozen', 'ruff', 'format', '--check', 'app', 'tests', '../db') } }
Gate 'backend types (mypy)' { In-Backend { Step uv @('run', '--frozen', 'mypy', 'app') } }
Gate 'backend import contracts' { In-Backend { Step uv @('run', '--frozen', 'lint-imports') } }

Gate 'backend migration check (up, back to 0001_baseline, up)' {
    $name = 'ci_local_migration_test'
    New-ScratchDatabase $name
    try {
        $env:DATABASE_URL_MIGRATE = Url $DbUser $DbPassword $name
        In-Backend {
            Step uv @('run', '--frozen', 'alembic', 'upgrade', 'head')
            Step uv @('run', '--frozen', 'alembic', 'downgrade', '0001_baseline')
            Step uv @('run', '--frozen', 'alembic', 'upgrade', 'head')
        }
    } finally {
        Remove-Item Env:DATABASE_URL_MIGRATE -ErrorAction SilentlyContinue
        Remove-ScratchDatabase $name
    }
}

Gate 'backend tests (pytest, database app_test)' {
    $env:TEST_DATABASE_URL = Url $DbUser $DbPassword 'app_test'
    try { In-Backend { Step uv @('run', '--frozen', 'pytest', 'tests', '-q') } }
    finally { Remove-Item Env:TEST_DATABASE_URL -ErrorAction SilentlyContinue }
}

Gate 'eval gate (harness_smoke: replay, then gate)' {
    $name = 'ci_local_evals_test'
    New-ScratchDatabase $name
    try {
        $env:DATABASE_URL_MIGRATE = Url $DbUser $DbPassword $name
        $env:DATABASE_URL_WORKER = Url 'app_worker' 'app_worker_local_only' $name
        $env:LLM_PROVIDER = 'fake'
        In-Backend {
            Step uv @('run', '--frozen', 'alembic', 'upgrade', 'head')
            $output = & uv run --frozen python -m app.evals run harness_smoke
            if ($LASTEXITCODE -ne 0) { throw "app.evals run exited with $LASTEXITCODE" }
            $runId = ($output | Select-Object -Last 1).ToString().Trim()  # the last line is the run id; log lines come first
            Step uv @('run', '--frozen', 'python', '-m', 'app.evals', 'gate', 'harness_smoke', '--run', $runId)
        }
    } finally {
        'DATABASE_URL_MIGRATE', 'DATABASE_URL_WORKER', 'LLM_PROVIDER' | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
        Remove-ScratchDatabase $name
    }
}

# ---- web -----------------------------------------------------------------------------------------------------------
$webReady = (Test-Path (Join-Path $web 'src')) -and (Test-Path (Join-Path $web 'openapi.json'))

Gate 'web install (pnpm install --frozen-lockfile)' {
    if (-not $webReady) { Skip 'the web app is not in the tree yet (plan 01-05)' }
    Push-Location $web
    try { Step pnpm @('install', '--frozen-lockfile') } finally { Pop-Location }
}

Gate 'web API types are current' {
    if (-not $webReady) { Skip 'the web app is not in the tree yet (plan 01-05)' }
    $check = Join-Path ([System.IO.Path]::GetTempPath()) 'openelip_openapi_check.py'
    @'
import json, pathlib, sys
from app.main import app
committed = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if json.loads(json.dumps(app.openapi())) != committed:
    sys.exit("web/openapi.json is out of date: regenerate it from app.openapi() and commit it")
'@ | Set-Content -Path $check -Encoding utf8
    try {
        Step uv @('run', '--frozen', '--project', $backend, 'python', $check, (Join-Path $web 'openapi.json'))
        Push-Location $web
        try { Step pnpm @('gen:api') } finally { Pop-Location }
        & git -C $repo diff --exit-code -- web/src/lib/api/schema.d.ts
        if ($LASTEXITCODE -ne 0) { throw 'web/src/lib/api/schema.d.ts is out of date: run pnpm gen:api and commit it' }
    } finally { Remove-Item $check -ErrorAction SilentlyContinue }
}

foreach ($webGate in @(
        @('web types (pnpm typecheck)', @('typecheck')),
        @('web lint (pnpm lint)', @('lint')),
        @('web tests (pnpm test --run)', @('test', '--run')))) {
    $label, $pnpmArguments = $webGate
    $body = {
        if (-not $webReady) { Skip 'the web app is not in the tree yet (plan 01-05)' }
        Push-Location $web
        try { Step pnpm $pnpmArguments } finally { Pop-Location }
    }.GetNewClosure()
    Gate $label $body
}

# ---- secret scan ---------------------------------------------------------------------------------------------------
Gate 'secret scan (gitleaks)' {
    if (-not (Get-Command gitleaks -ErrorAction SilentlyContinue)) { Skip 'gitleaks is not installed here; CI runs it on every pull request' }
    Step gitleaks @('git', '--no-banner', '--redact', $repo)  # the commits, as the CI job scans them (8.30.1 is pinned there)
}

# ---- summary -------------------------------------------------------------------------------------------------------
Write-Host "`n==== ci-local ====" -ForegroundColor Cyan
$results | ForEach-Object {
    $color = switch -Wildcard ($_) { 'PASS*' { 'Green' } 'FAIL*' { 'Red' } default { 'Yellow' } }
    Write-Host $_ -ForegroundColor $color
}
if ($failed -gt 0) { Write-Host "$failed gate(s) failed" -ForegroundColor Red; exit 1 }
Write-Host 'no gate failed'
exit 0
