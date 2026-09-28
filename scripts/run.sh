#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
    printf 'Missing virtual environment. Run: bash %q/scripts/install.sh\n' "$project_dir" >&2
    exit 1
fi

cd -- "$project_dir"
exec "$project_dir/.venv/bin/python" -m oak_camera "$@"
