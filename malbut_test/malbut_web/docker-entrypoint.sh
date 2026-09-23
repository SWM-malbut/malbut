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

# ECS/Fargate replaces the image-level HOSTNAME with the task hostname. Next's
# standalone server would then bind only to the task address, so the container
# health check on 127.0.0.1 could never reach it. Reset the bind address for the
# server process while leaving the container environment itself unchanged.
exec env HOSTNAME=0.0.0.0 node ./server.js
