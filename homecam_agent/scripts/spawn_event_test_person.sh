#!/usr/bin/env bash
set -euo pipefail

# Spawn the repository's obstacle-cleared Small House event actor. Do
# not replace this with the full-house perception demo or repeated `set_pose`
# calls: Gazebo actors are kinematic and can visibly cross furniture.
world=small_house
entity_name=event_clip_humanoid
timeout=60
readonly script_dir="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
  pwd -P
)"
readonly repository_root="$(
  cd -- "$script_dir/../.." >/dev/null 2>&1
  pwd -P
)"

while (($# > 0)); do
  case "$1" in
    --entity-name)
      entity_name="${2:?--entity-name requires a value}"
      shift 2
      ;;
    --timeout)
      timeout="${2:?--timeout requires a value}"
      shift 2
      ;;
    --world)
      world="${2:?--world requires a value}"
      shift 2
      ;;
    -h|--help)
      printf '%s\n' \
        'Usage: spawn_event_test_person.sh [--entity-name NAME] [--timeout SEC]' \
        'The obstacle-cleared event route is available only for small_house.'
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

if [[ "$world" != small_house ]]; then
  printf 'The event test person route is verified only for small_house.\n' >&2
  exit 2
fi
if [[ ! "$timeout" =~ ^[1-9][0-9]*$ ]]; then
  printf 'Timeout must be a positive integer.\n' >&2
  exit 2
fi
if [[ ! "$entity_name" =~ ^[A-Za-z][A-Za-z0-9_-]{0,62}$ ]]; then
  printf 'Entity name contains unsupported characters.\n' >&2
  exit 2
fi

if [[ -r /opt/ros/humble/setup.bash ]]; then
  # shellcheck disable=SC1091
  set +u
  source /opt/ros/humble/setup.bash
  set -u
fi

actor_file="$repository_root/malbut_gazebo/models/event_test_humanoid/model.sdf"
if [[ ! -r "$actor_file" ]]; then
  actor_file="$(
    ros2 pkg prefix --share malbut_gazebo
  )/models/event_test_humanoid/model.sdf"
  if [[ ! -r "$actor_file" ]]; then
    printf 'Cannot read the humanoid actor: %s\n' "$actor_file" >&2
    exit 1
  fi
fi

printf '%s\n' \
  'Spawning the Small House event circuit with a 0.8 m body envelope.' \
  'The actor walks, pauses, and turns in one open area; do not move it with set_pose.'

spawn_helper="$repository_root/malbut_gazebo/malbut_gazebo/spawn_when_ready.py"
if [[ -x "$spawn_helper" ]]; then
  spawn_command=(python3 "$spawn_helper")
else
  spawn_command=(ros2 run malbut_gazebo spawn_when_ready)
fi

exec "${spawn_command[@]}" \
  --world small_house \
  --entity-name "$entity_name" \
  --file "$actor_file" \
  --x 2.5 \
  --y -3.6 \
  --z 0 \
  --yaw 0 \
  --timeout "$timeout"
