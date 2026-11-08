#!/bin/sh
# The login in front of the whole app: HTTP basic auth from BASIC_AUTH_USER and
# BASIC_AUTH_PASSWORD. With REQUIRE_AUTH=1 (the private app) a missing password stops the
# container, so it never starts open by mistake.
set -eu
conf=/etc/nginx/pantry-auth.conf
if [ -n "${BASIC_AUTH_PASSWORD:-}" ]; then
  user="${BASIC_AUTH_USER:-pantry}"
  printf '%s:{PLAIN}%s\n' "$user" "$BASIC_AUTH_PASSWORD" > /etc/nginx/pantry.htpasswd
  chmod 640 /etc/nginx/pantry.htpasswd
  chown root:nginx /etc/nginx/pantry.htpasswd 2>/dev/null || true
  printf 'auth_basic "Pantry Planner";\nauth_basic_user_file /etc/nginx/pantry.htpasswd;\n' > "$conf"
elif [ "${REQUIRE_AUTH:-0}" = "1" ]; then
  echo "40-pantry-auth: REQUIRE_AUTH=1 but BASIC_AUTH_PASSWORD is empty; refusing to start open" >&2
  exit 1
else
  : > "$conf"
fi
