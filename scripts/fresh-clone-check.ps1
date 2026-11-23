<#
.SYNOPSIS
  Shows that a fresh clone starts with one command: clones the committed branch into a temporary folder, starts it as a
  separate Compose project, checks it, and removes everything it made. One PASS or FAIL line per step, exit code 1 if any failed.

.DESCRIPTION
  The running stack and its database are never touched. The copy gets its own Compose project (openelip_fresh), its own
  volumes and images, and other ports: database 5434, site 8081, Mailpit 8026, streaming check 8083. `docker compose
  down -v` runs only after the project name has been read back from Compose and compared with openelip_fresh.

  Only committed files are cloned, so commit before you run it.

  Steps: clone, start (docker compose up -d --wait), the site answers through the proxy (/v1/stats, /jobs), Mailpit
  answers, a streamed response arrives chunk by chunk through the same Caddyfile (ROADMAP research flag: the API has no
  streaming route yet, so a throwaway upstream sends two chunks three seconds apart), the 9 foundation tests pass, clean-up.
  -WithBrowserTests also runs the Playwright specs against the copy.

  Needs Docker, git and uv. Run it from PowerShell: it downloads images and packages.
#>
param(
    [switch]$WithBrowserTests,
    [string]$Project = 'openelip_fresh',
    [int]$HttpPort = 8081,
    [int]$MailPort = 8026,
    [int]$DbPort = 5434,
    [int]$StreamPort = 8083
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$start = Get-Location
$work = Join-Path ([IO.Path]::GetTempPath()) ("openelip-fresh-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$clone = Join-Path $work 'repo'
$streamNet = "${Project}_stream"
$streamApi = "${Project}_stream_api"
$streamProxy = "${Project}_stream_proxy"
$results = [System.Collections.Generic.List[string]]::new()
$failed = 0

function Step([string]$name, [scriptblock]$body) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    try {
        & $body
        $results.Add("PASS $name")
        Write-Host "PASS $name" -ForegroundColor Green
    } catch {
        $script:failed++
        $results.Add("FAIL $name")
        Write-Host "FAIL $name : $($_.Exception.Message)" -ForegroundColor Red
    }
}

# Runs a command, keeps its output, and shows the end of it only when the command fails.
function Run([string]$exe, [string[]]$arguments) {
    $output = & $exe @arguments 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        ($output -split "`r?`n" | Select-Object -Last 30) -join "`n" | Write-Host
        throw "$exe $($arguments -join ' ') exited with $LASTEXITCODE"
    }
    $output
}

function Get-Status([string]$url) {
    try { (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 20).StatusCode } catch { throw "$url did not answer: $($_.Exception.Message)" }
}

# Refuses to go on unless Compose itself says this folder is the throwaway project.
function Assert-FreshProject {
    $name = (Run docker @('compose', 'config', '--format', 'json') | ConvertFrom-Json).name
    if ($name -ne $Project -or $name -eq 'openelip') { throw "refusing to remove volumes: the project here is '$name', not '$Project'" }
}

try {
    Step 'clone the committed branch' {
        $branch = (git -C $repo branch --show-current).Trim()
        if (-not $branch) { throw 'no current branch (detached HEAD)' }
        Run git @('clone', '--quiet', '--single-branch', '--branch', $branch, $repo, $clone) | Out-Null
        $dirty = git -C $repo status --porcelain
        if ($dirty) { Write-Host "note: uncommitted changes are not in the clone:`n$dirty" -ForegroundColor Yellow }
        $envText = (Get-Content (Join-Path $clone '.env.example') -Raw) +
            "`nCOMPOSE_PROJECT_NAME=$Project`nAPP_HTTP_PORT=$HttpPort`nAPP_MAIL_PORT=$MailPort`nAPP_DB_PORT=$DbPort`n"
        [IO.File]::WriteAllText((Join-Path $clone '.env'), $envText)
    }

    if ($failed -eq 0) {
    Push-Location $clone
    Step 'docker compose up -d --wait (builds the images, migrates an empty database)' {
        Assert-FreshProject
        Run docker @('compose', 'up', '-d', '--wait', '--wait-timeout', '600') | Out-Null
    }
    Step "the site answers through the proxy: /v1/stats and /jobs on port $HttpPort" {
        if ((Get-Status "http://localhost:$HttpPort/v1/stats") -ne 200) { throw '/v1/stats is not 200' }
        if ((Get-Status "http://localhost:$HttpPort/jobs") -ne 200) { throw '/jobs is not 200' }
    }
    Step "Mailpit answers on port $MailPort" {
        if ((Get-Status "http://localhost:$MailPort/") -ne 200) { throw 'Mailpit is not 200' }
    }

    Step 'a streamed response is not held back by the proxy (same Caddyfile, throwaway upstream)' {
        $compose = Run docker @('compose', 'config', '--format', 'json') | ConvertFrom-Json
        $caddyImage = $compose.services.proxy.image
        $pythonImage = $compose.services.api.image
        $server = Join-Path $work 'stream_server.py'
        @'
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for n in (1, 2):
            data = f"data: chunk {n}\n\n".encode()
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()
            time.sleep(3)
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *args):
        pass

ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
'@ | Set-Content -Path $server -Encoding ascii
        Run docker @('network', 'create', $streamNet) | Out-Null
        Run docker @('run', '-d', '--name', $streamApi, '--network', $streamNet, '--network-alias', 'api',
            '-v', "${server}:/stream_server.py:ro", '--entrypoint', 'python', $pythonImage, '/stream_server.py') | Out-Null
        Run docker @('run', '-d', '--name', $streamProxy, '--network', $streamNet, '-p', "127.0.0.1:${StreamPort}:80",
            '-v', "$(Join-Path $clone 'Caddyfile'):/etc/caddy/Caddyfile:ro", $caddyImage) | Out-Null
        for ($i = 0; $i -lt 30 -and -not (Test-NetConnection 127.0.0.1 -Port $StreamPort -InformationLevel Quiet -WarningAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 500 }

        Add-Type -AssemblyName System.Net.Http  # Windows PowerShell 5.1 does not load it by default
        $client = [System.Net.Http.HttpClient]::new()
        $clock = [Diagnostics.Stopwatch]::StartNew()
        $response = $client.GetAsync("http://127.0.0.1:$StreamPort/v1/stream", [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        $reader = [IO.StreamReader]::new($response.Content.ReadAsStreamAsync().GetAwaiter().GetResult())
        $first = $null
        $chunks = 0
        while ($null -ne ($line = $reader.ReadLine())) {
            if ($line -like 'data:*') { $chunks++; if ($null -eq $first) { $first = $clock.Elapsed.TotalSeconds } }
        }
        $total = $clock.Elapsed.TotalSeconds
        $client.Dispose()
        Write-Host ("first chunk after {0:N1} s, last chunk and end of response after {1:N1} s, {2} chunks" -f $first, $total, $chunks)
        if ($chunks -ne 2) { throw "expected 2 chunks, got $chunks" }
        if ($first -gt 1.5) { throw "the first chunk waited $([math]::Round($first, 1)) s: the response was buffered" }
        if ($total -lt 5) { throw 'the upstream did not send the second chunk late, so this run proves nothing' }
    }

    Step 'foundation tests: uv run --frozen pytest tests/ingest/test_index.py (expects 9 passed)' {
        Push-Location (Join-Path $clone 'backend')
        try {
            # The copy's own database server, not the running stack's. conftest.py creates app_test there.
            $env:TEST_DATABASE_URL = "postgresql://app:app_local_only@127.0.0.1:$DbPort/app_test"
            Remove-Item Env:VIRTUAL_ENV -ErrorAction SilentlyContinue
            $output = Run uv @('run', '--frozen', 'pytest', 'tests/ingest/test_index.py', '-q')
            Write-Host (($output -split "`r?`n" | Select-Object -Last 3) -join "`n")
            if ($output -notmatch '\b9 passed\b') { throw 'did not see "9 passed"' }
        } finally {
            Remove-Item Env:TEST_DATABASE_URL -ErrorAction SilentlyContinue
            Pop-Location
        }
    }

    if ($WithBrowserTests) {
        Step 'browser tests (Playwright) against the copy' {
            Push-Location (Join-Path $repo 'web')
            try {
                $env:BASE_URL = "http://localhost:$HttpPort"
                $env:MAILPIT_URL = "http://localhost:$MailPort"
                $env:COMPOSE_PROJECT_NAME = $Project
                $env:E2E_COMPOSE_DIR = $clone
                # A throwaway stack: seed the invented jobs even after its worker has imported real boards, or the job specs skip.
                $env:E2E_SEED_JOBS = 'always'
                Run pnpm @('exec', 'playwright', 'test', 'tests/e2e', '--project=chromium') | Select-Object -Last 3 | Write-Host
            } finally {
                'BASE_URL', 'MAILPIT_URL', 'COMPOSE_PROJECT_NAME', 'E2E_COMPOSE_DIR', 'E2E_SEED_JOBS' | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }
                Pop-Location
            }
        }
    }
    }
} finally {
    Step "clean up: remove project $Project and its volumes, the temporary folder" {
        if (Test-Path $clone) {
            Push-Location $clone
            try {
                Assert-FreshProject
                Run docker @('compose', 'down', '-v', '--remove-orphans') | Out-Null
            } finally { Pop-Location }
        }
        foreach ($container in $streamProxy, $streamApi) { docker rm -f $container 2>&1 | Out-Null }
        docker network rm $streamNet 2>&1 | Out-Null
        docker rmi "${Project}-backend:dev" "${Project}-web:dev" 2>&1 | Out-Null
        Set-Location $start
        if (Test-Path $work) { Remove-Item -Recurse -Force $work }
        if (Test-Path $work) { throw "could not remove $work" }
    }
}

Write-Host "`n==== summary ====" -ForegroundColor Cyan
$results | ForEach-Object { Write-Host $_ }
if ($failed -gt 0) { Write-Host "$failed step(s) failed" -ForegroundColor Red; exit 1 }
Write-Host 'all steps passed' -ForegroundColor Green
