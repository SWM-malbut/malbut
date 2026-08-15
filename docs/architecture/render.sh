#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v dot >/dev/null 2>&1; then
  echo "Graphviz 'dot' is required." >&2
  exit 1
fi

for source in "$script_dir"/*.dot; do
  target="${source%.dot}.svg"
  dot -Tsvg "$source" -o "$target"
  echo "rendered $(basename "$target")"
done
