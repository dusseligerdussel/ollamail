# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# deps: install dependencies from pnpm-lock.yaml (pnpm version from package.json)
# ---------------------------------------------------------------------------
# Literal tags, not ARGs: Dependabot only updates literal FROM lines (.github/dependabot.yml).
FROM node:22-alpine AS deps

ENV PNPM_HOME=/pnpm \
    COREPACK_HOME=/opt/corepack \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0
ENV PATH="${PNPM_HOME}:${PATH}"
RUN corepack enable

WORKDIR /src
COPY package.json pnpm-lock.yaml .pnpmfile.cjs ./
RUN corepack install && chmod -R a+rX /opt/corepack
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile

# ---------------------------------------------------------------------------
# build: typecheck and produce the static bundle in /src/dist
# ---------------------------------------------------------------------------
FROM deps AS build
COPY . .
RUN pnpm build

# ---------------------------------------------------------------------------
# dev: Vite dev server with hot reload, source is bind-mounted by deploy/compose.dev.yaml
# ---------------------------------------------------------------------------
FROM deps AS dev
RUN chown -R 1000:1000 /src
USER 1000:1000
EXPOSE 5173
CMD ["pnpm", "dev", "--host", "0.0.0.0", "--port", "5173", "--strictPort"]

# ---------------------------------------------------------------------------
# proxy: Caddy as unprivileged user, without the bundle (used by compose.dev.yaml)
# ---------------------------------------------------------------------------
FROM caddy:2.11-alpine AS proxy

# Caddy keeps its state in XDG directories; point them to a writable location so the
# container also works with a read-only root filesystem (only /tmp needs to be writable).
ENV XDG_CONFIG_HOME=/tmp/caddy/config \
    XDG_DATA_HOME=/tmp/caddy/data

# Caddy listens on unprivileged port 8080. Re-copying the binary drops its
# cap_net_bind_service file capability, which would otherwise make exec fail under
# `cap_drop: ALL` / no-new-privileges.
RUN cp /usr/bin/caddy /usr/bin/caddy.tmp \
    && mv /usr/bin/caddy.tmp /usr/bin/caddy \
    && addgroup -S -g 10001 ollamail \
    && adduser -S -u 10001 -G ollamail -H -h /srv -s /sbin/nologin ollamail

COPY caddy/Caddyfile /etc/caddy/Caddyfile

USER 10001:10001
EXPOSE 8080
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]

# ---------------------------------------------------------------------------
# runtime (default target): static bundle served by Caddy, /api proxied to the backend
# ---------------------------------------------------------------------------
FROM proxy AS runtime
COPY --from=build /src/dist /srv
