# OpenElip installer for Windows (PowerShell 5.1 or later):
#   irm https://raw.githubusercontent.com/jaiswal-naman/openelip/main/install.ps1 | iex
#
# It needs Docker Desktop and nothing else. It puts three files in %USERPROFILE%\openelip (docker-compose.yml, Caddyfile,
# .env), pulls the published images, starts them, and opens http://localhost:8080. Running it again updates to the newest
# images and keeps your data and your .env. Everything stays on this computer: the ports are bound to 127.0.0.1.
#
# Settings, all optional, as environment variables: OPENELIP_HOME, OPENELIP_PORT (8080), OPENELIP_VERSION (latest),
# OPENELIP_PROJECT (openelip), OPENELIP_PULL (0 to use images already on this machine).
# Everything runs in its own scope, so `| iex` leaves the caller's session as it was.
& {
  $ErrorActionPreference = "Stop"
  [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

  function Get-Setting($Name, $Default) { $v = [Environment]::GetEnvironmentVariable($Name); if ($v) { $v } else { $Default } }

  $Repo = "jaiswal-naman/openelip"
  $Raw = Get-Setting "OPENELIP_RAW" "https://raw.githubusercontent.com/$Repo/main"
  $HomeDir = Get-Setting "OPENELIP_HOME" (Join-Path $env:USERPROFILE "openelip")
  $Port = Get-Setting "OPENELIP_PORT" "8080"
  $Version = Get-Setting "OPENELIP_VERSION" "latest"
  $Project = Get-Setting "OPENELIP_PROJECT" "openelip"
  $Url = "http://localhost:$Port"

  function Write-Step($Text) { Write-Host "==> " -ForegroundColor White -NoNewline; Write-Host $Text }
  function Stop-Install($Text) { Write-Host "Error: $Text" -ForegroundColor Red; throw "OpenElip install stopped" }
  function Get-RandomBytes { $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); $b }
  function Get-RandomHex { -join (Get-RandomBytes | ForEach-Object { $_.ToString("x2") }) }
  function Get-FernetKey { [Convert]::ToBase64String((Get-RandomBytes)).Replace("+", "-").Replace("/", "_") }
  function Save-Download($Name, $Target) {
    try { Invoke-WebRequest -UseBasicParsing -Uri "$Raw/$Name" -OutFile $Target }
    catch { Stop-Install "could not download $Name from $Raw" }
  }
  function Test-Native { param([scriptblock]$Command) $ErrorActionPreference = "Continue"; & $Command *> $null; $LASTEXITCODE -eq 0 }

  Write-Host ""
  Write-Host "OpenElip" -NoNewline -ForegroundColor White
  Write-Host " - your job-search crew, on your own computer" -ForegroundColor DarkGray
  Write-Host ""

  Write-Step "Checking Docker"
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Stop-Install "Docker is not installed. Install Docker Desktop (https://docs.docker.com/desktop/setup/install/windows-install/), start it, and run this again."
  }
  if (-not (Test-Native { docker compose version })) { Stop-Install "Docker Compose v2 is missing. Update Docker Desktop." }
  if (-not (Test-Native { docker info })) { Stop-Install "Docker is installed but not running. Start Docker Desktop and run this again." }

  New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
  Set-Location $HomeDir
  $HomeDir = (Get-Location).Path

  # Never take over a Compose project that lives somewhere else, such as a source checkout with its own database.
  # The labels are read as JSON: Windows PowerShell drops the double quotes a --format template with .Label would need.
  $ids = docker ps -a -q --filter "label=com.docker.compose.project=$Project"
  if ($LASTEXITCODE -ne 0) { Stop-Install "could not list the Docker containers." }
  $other = $ids | ForEach-Object { (docker inspect --format '{{json .Config.Labels}}' $_ | ConvertFrom-Json).'com.docker.compose.project.working_dir' } |
    Sort-Object -Unique | Where-Object { $_ -and $_ -ne $HomeDir }
  if ($other) { Stop-Install "a Docker project called '$Project' already runs from $other. Choose another name: set OPENELIP_PROJECT=openelip2" }

  Write-Step "Downloading to $HomeDir"
  foreach ($f in "docker-compose.yml", "Caddyfile") { Save-Download $f "$f.tmp"; Move-Item -Force "$f.tmp" $f }

  $utf8 = New-Object Text.UTF8Encoding $false  # Compose reads .env as UTF-8 without a byte order mark
  if (Test-Path .env) {
    # Your keys stay: new ones would make everything already saved unreadable.
    Write-Step "Keeping your settings in $HomeDir\.env"
  } else {
    Write-Step "Writing settings with fresh keys to $HomeDir\.env"
    Save-Download ".env.example" ".env.tmp"
    $set = @{
      APP_ENV = "production"; APP_BASE_URL = $Url; APP_DATA_KEYS = Get-FernetKey; APP_LOOKUP_KEY = Get-RandomHex
      APP_LINK_KEY = Get-RandomHex; APP_DB_API_PASSWORD = Get-RandomHex; APP_DB_WORKER_PASSWORD = Get-RandomHex
    }
    $lines = [IO.File]::ReadAllLines((Join-Path $HomeDir ".env.tmp")) | ForEach-Object {
      $name = ($_ -split "=", 2)[0]
      if ($set.ContainsKey($name)) { "$name=$($set[$name])" } else { $_ }
    }
    $lines += @(
      "", "# Written by install.ps1", "COMPOSE_PROJECT_NAME=$Project", "APP_HTTP_PORT=$Port",
      "APP_DB_MATCH_PASSWORD=$(Get-RandomHex)",
      "OPENELIP_BACKEND_IMAGE=ghcr.io/$Repo-backend:$Version", "OPENELIP_WEB_IMAGE=ghcr.io/$Repo-web:$Version"
    )
    [IO.File]::WriteAllText((Join-Path $HomeDir ".env"), (($lines -join "`n") + "`n"), $utf8)
    Remove-Item .env.tmp
  }

  if ((Get-Setting "OPENELIP_PULL" "1") -ne "0") {
    Write-Step "Pulling the images (the first time takes a few minutes)"
    docker compose pull
    if ($LASTEXITCODE -ne 0) { Stop-Install "could not pull the images. Check your internet connection and run this again." }
  }

  Write-Step "Starting OpenElip"
  docker compose up -d --no-build --remove-orphans
  if ($LASTEXITCODE -ne 0) {
    Stop-Install "could not start. If a port is taken, set APP_HTTP_PORT or APP_DB_PORT in $HomeDir\.env and run: docker compose up -d"
  }

  Write-Step "Waiting for it to answer at $Url"
  $deadline = (Get-Date).AddMinutes(4)
  while ($true) {
    try { if ((Invoke-WebRequest -UseBasicParsing -Uri "$Url/healthz" -TimeoutSec 5).StatusCode -eq 200) { break } } catch { }
    if ((Get-Date) -gt $deadline) {
      Stop-Install "it did not answer within 4 minutes. See what happened with: cd $HomeDir; docker compose logs --tail 50"
    }
    Start-Sleep -Seconds 2
  }

  Write-Host ""
  Write-Host "OpenElip is running at $Url" -ForegroundColor White
  Write-Host ""
  Write-Host "  Open it        $Url"
  Write-Host "  Stop it        cd $HomeDir; docker compose stop" -ForegroundColor DarkGray
  Write-Host "  Update it      run this installer again" -ForegroundColor DarkGray
  Write-Host "  Remove it      cd $HomeDir; docker compose down -v  (deletes your data)" -ForegroundColor DarkGray
  Write-Host ""
  Start-Process $Url
}
