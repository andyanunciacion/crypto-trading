# Build context is frontend/ (see compose.yaml).
# Multi-stage build: install and build in throwaway stages, then copy only the
# standalone server into a small final image.
FROM node:24.22.0-alpine AS base
ENV NEXT_TELEMETRY_DISABLED=1
# corepack installs the exact pnpm version named in package.json's "packageManager".
RUN corepack enable
WORKDIR /app

FROM base AS deps
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

FROM node:24.22.0-alpine AS runner
WORKDIR /app
# HOSTNAME=0.0.0.0 makes the server listen on all interfaces inside the container.
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
# The node image ships an unprivileged "node" user.
USER node
EXPOSE 3000
CMD ["node", "server.js"]
