#!/usr/bin/env bash
# Run the stdlib-only CI contract suites from any working directory.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
for test_file in "$script_dir"/test-*.py; do
  python3 "$test_file"
done
printf '%s\n' 'CI selection and workflow checks passed.'
