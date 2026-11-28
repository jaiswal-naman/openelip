#!/bin/sh
# OpenElip installer for macOS and Linux:
#   curl -fsSL https://raw.githubusercontent.com/jaiswal-naman/openelip/main/install.sh | sh
#
# It needs Docker with Compose and nothing else. It puts three files in ~/openelip (docker-compose.yml, Caddyfile, .env),
# pulls the published images, starts them, and opens http://localhost:8080. Running it again updates to the newest
# images and keeps your data and your .env. Everything stays on this computer: the ports are bound to 127.0.0.1.
#
# Settings, all optional:
#   OPENELIP_HOME     where the files go (default: ~/openelip)
#   OPENELIP_PORT     the port the app answers on (default: 8080)
#   OPENELIP_VERSION  the image tag to run (default: latest)
#   OPENELIP_PROJECT  the Docker Compose project name (default: openelip)
#   OPENELIP_PULL     0 to use images already on this machine instead of pulling them
set -eu

REPO="jaiswal-naman/openelip"
RAW="${OPENELIP_RAW:-https://raw.githubusercontent.com/$REPO/main}"
HOME_DIR="${OPENELIP_HOME:-$HOME/openelip}"
PORT="${OPENELIP_PORT:-8080}"
VERSION="${OPENELIP_VERSION:-latest}"
PROJECT="${OPENELIP_PROJECT:-openelip}"
URL="http://localhost:$PORT"

if [ -t 1 ]; then B=$(printf '\033[1m'); D=$(printf '\033[2m'); R=$(printf '\033[31m'); N=$(printf '\033[0m'); else B= D= R= N=; fi
step() { printf '%s==>%s %s\n' "$B" "$N" "$1"; }
fail() { printf '%sError:%s %s\n' "$R" "$N" "$1" >&2; exit 1; }
random_hex() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
fernet_key() { head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_'; }

printf '\n%sOpenElip%s %s- your job-search crew, on your own computer%s\n\n' "$B" "$N" "$D" "$N"

step "Checking Docker"
command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Install Docker Desktop (https://docs.docker.com/get-docker/), start it, and run this again."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is missing. Update Docker Desktop, or install the compose plugin."
docker info >/dev/null 2>&1 || fail "Docker is installed but not running. Start Docker Desktop and run this again."
command -v curl >/dev/null 2>&1 || fail "curl is needed to download the files."

mkdir -p "$HOME_DIR"
cd "$HOME_DIR"
HOME_DIR=$(pwd)

# Never take over a Compose project that lives somewhere else, such as a source checkout with its own database.
OTHER=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
  --format '{{.Label "com.docker.compose.project.working_dir"}}' | sort -u | grep -vxF "$HOME_DIR" || true)
[ -z "$OTHER" ] || fail "a Docker project called '$PROJECT' already runs from $OTHER. Choose another name with OPENELIP_PROJECT=openelip2"

step "Downloading to $HOME_DIR"
for f in docker-compose.yml Caddyfile; do
  curl -fsSL "$RAW/$f" -o "$f.tmp" || fail "could not download $f from $RAW"
  mv "$f.tmp" "$f"
done

if [ -f .env ]; then
  # Your keys stay: new ones would make everything already saved unreadable.
  step "Keeping your settings in $HOME_DIR/.env"
else
  step "Writing settings with fresh keys to $HOME_DIR/.env"
  curl -fsSL "$RAW/.env.example" -o .env.tmp || fail "could not download .env.example"
  sed -e "s|^APP_ENV=.*|APP_ENV=production|" \
      -e "s|^APP_BASE_URL=.*|APP_BASE_URL=$URL|" \
      -e "s|^APP_DATA_KEYS=.*|APP_DATA_KEYS=$(fernet_key)|" \
      -e "s|^APP_LOOKUP_KEY=.*|APP_LOOKUP_KEY=$(random_hex)|" \
      -e "s|^APP_LINK_KEY=.*|APP_LINK_KEY=$(random_hex)|" \
      -e "s|^APP_DB_API_PASSWORD=.*|APP_DB_API_PASSWORD=$(random_hex)|" \
      -e "s|^APP_DB_WORKER_PASSWORD=.*|APP_DB_WORKER_PASSWORD=$(random_hex)|" \
      .env.tmp > .env
  rm -f .env.tmp
  {
    printf '\n# Written by install.sh\n'
    printf 'COMPOSE_PROJECT_NAME=%s\n' "$PROJECT"
    printf 'APP_HTTP_PORT=%s\n' "$PORT"
    printf 'APP_DB_MATCH_PASSWORD=%s\n' "$(random_hex)"
    printf 'OPENELIP_BACKEND_IMAGE=ghcr.io/%s-backend:%s\n' "$REPO" "$VERSION"
    printf 'OPENELIP_WEB_IMAGE=ghcr.io/%s-web:%s\n' "$REPO" "$VERSION"
  } >> .env
  chmod 600 .env
fi

if [ "${OPENELIP_PULL:-1}" != 0 ]; then
  step "Pulling the images (the first time takes a few minutes)"
  docker compose pull || fail "could not pull the images. Check your internet connection and run this again."
fi

step "Starting OpenElip"
docker compose up -d --no-build --remove-orphans || fail "could not start. If a port is taken, set APP_HTTP_PORT or APP_DB_PORT in $HOME_DIR/.env and run: docker compose up -d"

step "Waiting for it to answer at $URL"
i=0
until curl -fsS "$URL/healthz" >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -le 120 ] || fail "it did not answer within 4 minutes. See what happened with: cd $HOME_DIR && docker compose logs --tail 50"
  sleep 2
done

printf '\n%sOpenElip is running at %s%s\n\n' "$B" "$URL" "$N"
printf '  Open it        %s\n' "$URL"
printf '  Stop it        %scd %s && docker compose stop%s\n' "$D" "$HOME_DIR" "$N"
printf '  Update it      %srun this installer again%s\n' "$D" "$N"
printf '  Remove it      %scd %s && docker compose down -v%s  (deletes your data)\n\n' "$D" "$HOME_DIR" "$N"

if command -v open >/dev/null 2>&1; then open "$URL" >/dev/null 2>&1 || true
elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
fi
