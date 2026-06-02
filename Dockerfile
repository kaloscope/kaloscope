# ============================================================
# Stage 1: build the frontend
# ============================================================
FROM node:24-slim AS frontend

# install pnpm via npm
RUN npm install -g pnpm@latest-11

WORKDIR /pages

# copy dependency manifests first for better layer caching
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./

# install frontend dependencies
RUN pnpm install --frozen-lockfile

# copy the rest of the frontend source code
COPY frontend/ ./

# build the static pages for production
RUN pnpm run build

# ============================================================
# Stage 2: build the production image
# ============================================================
FROM python:3.13-slim

# install runtime dependencies
# - git:               required by gitpython
# - libxml2, libxslt:  required by lxml
# - gosu:              used to drop privileges
# - aria2:             optional download manager
# - curl:              used to download mkcert at runtime
# - libnss3-tools:     required by mkcert to install CA certificates
# - media-types:       required to guess MIME types of files based on their extensions
# - ffmpeg:            used to transcode videos for streaming
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libxml2 \
    libxslt1.1 \
    gosu \
    aria2 \
    curl \
    libnss3-tools \
    media-types \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# download and install mkcert
ARG TARGETPLATFORM
RUN curl -fsSL "https://dl.filippo.io/mkcert/latest?for=${TARGETPLATFORM}" -o /usr/local/bin/mkcert \
    && chmod +x /usr/local/bin/mkcert

# install poetry via pip
RUN python -m pip install --no-cache-dir setuptools "poetry~=2.0"

WORKDIR /app

# copy backend dependency manifests first for better layer caching
COPY backend/pyproject.toml backend/poetry.lock backend/poetry.toml ./backend/

# install backend dependencies, then remove build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake make g++ \
    && cd backend \
    && poetry install --no-root --no-cache --no-interaction --only main \
    && apt-get purge -y cmake make g++ \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# copy the rest of the backend source code
COPY backend/ ./backend/

# copy the built frontend assets from the previous stage
COPY --from=frontend /pages/build/ ./frontend/build/

# environment variables
ENV CONTAINER=true
ENV PUID=0
ENV PGID=0
ENV UMASK=022
ENV TLS_HOSTNAME=
ENV AUTO_TLS=false
ENV DEBUG_MODE=false
ENV ENABLE_ARIA2=false

# entrypoint script
COPY <<'EOF' /app/entrypoint.sh
#!/bin/sh
set -e
cd /app/backend

# apply umask
umask ${UMASK:-022}

# setup user if PUID/PGID is set
PUID=${PUID:-0}
PGID=${PGID:-0}
if [ "$PUID" != "0" ] && [ "$(id -u)" = "0" ]; then
  getent group "$PGID" > /dev/null 2>&1 || groupadd -g "$PGID" kaloscope
  id -u "$PUID" > /dev/null 2>&1 || useradd -u "$PUID" -g "$PGID" -m -s /bin/sh kaloscope
  chown -R "$PUID":"$PGID" /app /workspace
  exec gosu "$PUID" "$0" "$@"
fi

# set up root CA path for mkcert if AUTO_TLS is enabled
if [ "$AUTO_TLS" = "true" ]; then
  export CAROOT=/workspace/mkcert
  mkdir -p "$CAROOT"
fi

# start aria2 if enabled
if [ "$ENABLE_ARIA2" = "true" ]; then
  mkdir -p /workspace/aria2
  ARIA2_SESSION=/workspace/aria2/aria2.session
  touch "$ARIA2_SESSION"
  aria2c \
    --enable-rpc \
    --rpc-listen-all=false \
    --rpc-listen-port=6800 \
    --enable-peer-exchange=true \
    --enable-dht=true \
    --dht-listen-port=6888 \
    --listen-port=6888 \
    --input-file="$ARIA2_SESSION" \
    --save-session="$ARIA2_SESSION" \
    --save-session-interval=60 \
    --daemon
fi

# start sanic web server
SANIC_ARGS="--host 0.0.0.0 --port 8000 --fast"
if [ "$AUTO_TLS" = "true" ]; then
  SANIC_ARGS="$SANIC_ARGS --auto-tls"
fi
if [ "$DEBUG_MODE" = "true" ]; then
  SANIC_ARGS="$SANIC_ARGS --debug"
fi
exec poetry run sanic app.main:app $SANIC_ARGS
EOF
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000
EXPOSE 6888
EXPOSE 6888/udp
VOLUME /workspace
ENTRYPOINT ["/app/entrypoint.sh"]
