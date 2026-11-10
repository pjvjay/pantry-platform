# The console for Render (context: the repo root): pantry-frontend's build from the pinned
# submodule, served by nginx with routes to pantry-api and the hub and an optional login.
FROM node:20-alpine AS build
WORKDIR /app
COPY pantry-frontend/package.json pantry-frontend/package-lock.json ./
RUN npm ci
COPY pantry-frontend/tsconfig.json pantry-frontend/vite.config.ts pantry-frontend/index.html ./
COPY pantry-frontend/src/ ./src/
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/web/default.conf.template /etc/nginx/templates/default.conf.template
COPY deploy/web/40-pantry-auth.sh /docker-entrypoint.d/40-pantry-auth.sh
COPY --from=build /app/dist /usr/share/nginx/html
# Only these variables are substituted, so nginx's own $host and friends stay as they are.
# NGINX_ENTRYPOINT_LOCAL_RESOLVERS makes the image's entrypoint export the container's
# nameservers as NGINX_LOCAL_RESOLVERS, which the template's `resolver` uses to look APP_HOST
# up again whenever pantry-app moves.
ENV PORT=10000 APP_HOST=pantry-app NGINX_ENTRYPOINT_LOCAL_RESOLVERS=true \
    NGINX_ENVSUBST_FILTER="^(PORT|APP_HOST|NGINX_LOCAL_RESOLVERS)$"
RUN rm -f /etc/nginx/conf.d/default.conf
EXPOSE 10000
