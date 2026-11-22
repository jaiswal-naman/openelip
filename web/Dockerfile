# The web app as a production image: Next.js standalone output, run by Node as a user with no shell login.
# Build context is this folder:  docker build -t openelip-web:dev web
#
# The base image is pinned by digest, so a rebuild cannot silently pick up a different Node. To move to a newer patch release,
# pull node:22-bookworm-slim, read its digest with `docker inspect --format '{{index .RepoDigests 0}}'`, and change it below.
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee

# --- dependencies: from the lock file only, so this layer is reused until package.json or the lock changes
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
# corepack reads "packageManager" from package.json and fetches that exact pnpm
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# --- build: next build writes a self-contained server to .next/standalone
FROM ${NODE_IMAGE} AS build
WORKDIR /app
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 NEXT_TELEMETRY_DISABLED=1
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# next/font downloads Inter and JetBrains Mono here, so the running app never calls a font service
RUN pnpm build

# --- runtime: only what the standalone server needs
FROM ${NODE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN groupadd --system --gid 1001 app && useradd --system --uid 1001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
USER app
EXPOSE 3000
# API_INTERNAL_URL (for example http://api:8000) is read at run time by the server-rendered pages
CMD ["node", "server.js"]
