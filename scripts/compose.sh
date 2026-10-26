#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Run Docker Compose for exactly one environment.
# Usage: scripts/compose.sh <env-file> <compose arguments...>
# Validates the env file, then runs the project
# rootkeel-<RT_ENVIRONMENT> with values taken only from that file. Missing
# docker plugins are fetched into $RT_DOCKER_CONFIG_DIR, default
# <state>/docker-config.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
[ $# -ge 1 ] || { echo 'usage: scripts/compose.sh <env-file> <compose arguments...>' >&2; exit 64; }
[ -f "$1" ] || { echo "Environment file not found: $1" >&2; exit 2; }
env_file=$(realpath "$1")
shift
setting() { PYTHONPATH="$here" python3 -m rootkeel.config get "$env_file" "$1"; }
PYTHONPATH="$here" python3 -m rootkeel.config check-env "$env_file" >&2
environment=$(setting RT_ENVIRONMENT)
plugins=${RT_DOCKER_CONFIG_DIR:-$(setting RT_STATE_DIR)/docker-config}
# Compose prefers shell variables over --env-file values; only the file may decide.
while IFS= read -r name; do
  unset "$name"
done < <(compgen -e | grep '^RT_' || true)

# shellcheck source-path=SCRIPTDIR source=lib/docker-plugins.sh
. "$here/scripts/lib/docker-plugins.sh"
ensure_docker_plugins "$plugins"
exec docker compose --project-name "rootkeel-$environment" --env-file "$env_file" -f "$here/server/compose.yaml" "$@"
