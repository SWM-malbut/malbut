#!/usr/bin/env bash
set -euo pipefail

web=false
infra=false
ros=false
homecam=false

select_path()
{
  local path="$1"
  case "$path" in
    .github/workflows/ci.yml|.github/scripts/*ci-modules*)
      web=true
      infra=true
      ros=true
      homecam=true
      ;;
    homecam_web/infra/*)
      web=true
      infra=true
      ;;
    homecam_web/*)
      web=true
      ;;
    homecam_agent/*)
      homecam=true
      ;;
    malbut_description/*|malbut_gazebo/*)
      ros=true
      homecam=true
      ;;
    malbut_agent_server/*|malbut_autonomy/*)
      ros=true
      ;;
  esac
}

if [ "${1:-}" = "--paths" ]; then
  shift
  for path in "$@"; do
    select_path "$path"
  done
else
  base_sha="${1:-}"
  if [ -z "$base_sha" ] || [[ "$base_sha" =~ ^0+$ ]] ||
    ! git cat-file -e "${base_sha}^{commit}" 2>/dev/null
  then
    base_sha="$(git rev-parse HEAD^)"
  fi
  while IFS= read -r -d '' path; do
    select_path "$path"
  done < <(git diff --name-only --diff-filter=ACMRD -z "$base_sha" HEAD)
fi

printf 'web=%s\n' "$web"
printf 'infra=%s\n' "$infra"
printf 'ros=%s\n' "$ros"
printf 'homecam=%s\n' "$homecam"
