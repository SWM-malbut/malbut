#!/bin/sh
set -eu

attempt=1
max_attempts=12
while ! node ./scripts/migrate.mjs; do
  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "PostgreSQL migration failed after ${max_attempts} attempts." >&2
    exit 1
  fi

  delay=$((attempt * 2))
  if [ "$delay" -gt 30 ]; then
    delay=30
  fi
  echo "PostgreSQL is not ready; retrying migration in ${delay}s (${attempt}/${max_attempts})." >&2
  sleep "$delay"
  attempt=$((attempt + 1))
done

exec node ./server.js
