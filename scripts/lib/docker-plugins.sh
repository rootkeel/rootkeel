# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-only
# Sourced by the Compose and image-build wrappers. Uses the host's `docker compose`
# and `docker buildx` plugins when both are present; otherwise fetches pinned,
# checksum-verified plugins into "$1"/cli-plugins and points DOCKER_CONFIG there,
# so nothing is installed system-wide. The operator's config.json (registry
# logins and credential helpers) and contexts/ are linked into "$1", so registry
# access and the current Docker context are kept.
COMPOSE_VERSION=5.5.1
BUILDX_VERSION=0.37.1
fetch_plugin() { # url sha256 dest
  [ -x "$3" ] && return 0
  local tmp="$3.tmp.$$"
  curl -fsSLo "$tmp" "$1"
  echo "$2  $tmp" | sha256sum -c - >&2
  chmod 0755 "$tmp" && mv "$tmp" "$3"
}

ensure_docker_plugins() { # docker-config-dir
  if docker compose version >/dev/null 2>&1 && docker buildx version >/dev/null 2>&1; then
    return 0
  fi
  # Plain variables rather than associative arrays: macOS ships bash 3.2.
  local arch carch compose_sha buildx_sha plugins config entry
  case "$(uname -m)" in
    x86_64 | amd64)
      arch=amd64; carch=x86_64
      compose_sha=db1889184726840f75c4f9c001048430d4f25b3be3cb084d3ddd762bc0aed576
      buildx_sha=9447199cdb435f25880548343c128a4b6650e8891ee598905d8d29d39a8e359b ;;
    aarch64 | arm64)
      arch=arm64; carch=aarch64
      compose_sha=732e3a84c1a0f67256ce80bc2598a24546b10ca05f9faa97efceb1171ece2ef7
      buildx_sha=e5cc9fe3bbff5cbc91230981f7860e06076110730a2db997082652199042a1f2 ;;
    *) echo "unsupported arch $(uname -m)" >&2; return 1 ;;
  esac
  plugins="$1/cli-plugins"
  mkdir -p "$plugins"
  fetch_plugin "https://github.com/docker/compose/releases/download/v$COMPOSE_VERSION/docker-compose-linux-$carch" \
    "$compose_sha" "$plugins/docker-compose"
  fetch_plugin "https://github.com/docker/buildx/releases/download/v$BUILDX_VERSION/buildx-v$BUILDX_VERSION.linux-$arch" \
    "$buildx_sha" "$plugins/docker-buildx"
  # An account without a Docker config (no ~/.docker) has nothing to link.
  config=$(cd "${DOCKER_CONFIG:-$HOME/.docker}" 2>/dev/null && pwd) || config=
  for entry in config.json contexts; do
    if [ -e "$config/$entry" ] && ! [ "$config/$entry" -ef "$1/$entry" ]; then
      ln -sfn "$config/$entry" "$1/$entry"
    fi
  done
  export DOCKER_CONFIG="$1"
}
