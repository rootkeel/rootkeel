#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Build the broker and relay images for one immutable version, with their OCI
# labels, SBOMs and build provenance (lib/image-build.sh).
# Usage: scripts/build-images.sh [--push] <repository-prefix> <version-tag>
#   e.g. scripts/build-images.sh registry.example.com/rootkeel 0.1.0-git-$(git rev-parse --short=12 HEAD)
# Without --push the images are loaded into the local Docker image store and
# nothing is pushed. With --push they are built by a BuildKit container (the
# rootkeel-attest builder, created if missing) and pushed with an SBOM (syft)
# and SLSA provenance attestation attached to each image.
# Either way, each image's SPDX SBOM and provenance are written as
# <name>.spdx.json and <name>.provenance.json to
# ~/.cache/rootkeel/images/<version-tag>.
# RT_IMAGE_PLATFORMS (with --push only), for example linux/amd64,linux/arm64,
# builds each image for those platforms as one multi-platform image; its SBOMs
# and provenance are then written per platform, as
# <name>.<os>-<arch>.spdx.json and <name>.<os>-<arch>.provenance.json.
# Platforms other than the builder's own need QEMU emulation registered on the
# Docker host (binfmt_misc).
# Build once, then promote the same image between environments by pinning its
# reference (preferably its registry digest) in each environment's file.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
push=
if [ "${1:-}" = --push ]; then push=1; shift; fi
prefix=${1:?repository prefix required, for example registry.example.com/rootkeel}
tag=${2:?immutable version tag required, for example 0.1.0-git-<commit>}
prefix=${prefix%/}
platforms=${RT_IMAGE_PLATFORMS:-}
if [ -n "$platforms" ] && [ -z "$push" ]; then
  echo 'RT_IMAGE_PLATFORMS needs --push: the local image store keeps one platform per image' >&2
  exit 64
fi
for name in broker relay; do
  PYTHONPATH="$here" python3 -c 'import sys; from rootkeel.config import image_ref, ConfigError
try: image_ref(sys.argv[1], "image")
except ConfigError as e: sys.exit(str(e))' "$prefix/$name:$tag"
done
# shellcheck source-path=SCRIPTDIR source=lib/docker-plugins.sh
. "$here/scripts/lib/docker-plugins.sh"
# shellcheck source-path=SCRIPTDIR source=lib/image-build.sh
. "$here/scripts/lib/image-build.sh"
ensure_docker_plugins "${RT_DOCKER_CONFIG_DIR:-$HOME/.cache/rootkeel/docker-config}"
set_image_labels "$here" "$tag"
evidence=$HOME/.cache/rootkeel/images/$tag
mkdir -p "$evidence"
output=(--load)
if [ -n "$push" ]; then
  builder=rootkeel-attest
  docker buildx inspect "$builder" >/dev/null 2>&1 \
    || docker buildx create --name "$builder" --driver docker-container --driver-opt "image=$BUILDKIT_IMAGE" >/dev/null
  # The pushed image is an index (image plus attestations): GitHub links a
  # ghcr.io package to its repository from the index's source annotation,
  # not from the image's label.
  output=(--builder "$builder" --push --provenance=mode=max --sbom="generator=$SBOM_GENERATOR"
    --annotation "index:org.opencontainers.image.source=$image_source")
  if [ -n "$platforms" ]; then output+=(--platform "$platforms"); fi
fi
exists() { # ref
  local out
  docker image inspect "$1" >/dev/null 2>&1 && return 0
  [ -n "$push" ] || return 1
  out=$(docker buildx imagetools inspect --builder "$builder" "$1" 2>&1) && return 0
  case "$(printf '%s' "$out" | tr '[:upper:]' '[:lower:]')" in
    *"not found"* | *authoriz* | *denied* | *insufficient_scope*) return 1 ;;
    *) echo "cannot check whether $1 is in the registry: $out" >&2; exit 1 ;;
  esac
}
for ref in "$prefix/broker:$tag" "$prefix/relay:$tag"; do
  if exists "$ref"; then
    echo "$ref already exists; version tags are immutable, choose a new version" >&2
    exit 1
  fi
done
build() { # name dockerfile [build options...]
  local name=$1 dockerfile=$2
  shift 2
  BUILDX_METADATA_PROVENANCE=max docker buildx build "${output[@]}" "${image_labels[@]}" \
    --label "org.opencontainers.image.title=rootkeel-$name" \
    --metadata-file "$evidence/$name.metadata.json" \
    -f "$here/images/$dockerfile" -t "$prefix/$name:$tag" "$@" "$here"
}
build broker broker.Dockerfile --target runtime
build relay relay.Dockerfile
for name in broker relay; do
  if [ -n "$platforms" ]; then
    # Per platform: the provenance BuildKit recorded, and the attached SBOM as the registry serves it.
    digest=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$evidence/$name.metadata.json")
    docker buildx imagetools inspect --builder "$builder" "$prefix/$name@$digest" --format '{{ json .SBOM }}' > "$evidence/$name.sbom-index.json"
    python3 - "$evidence" "$name" "$platforms" <<'PY'
import json, sys
from pathlib import Path
evidence, name, platforms = Path(sys.argv[1]), sys.argv[2], sys.argv[3].split(",")
metadata = json.loads((evidence / (name + ".metadata.json")).read_text())
sboms = json.loads((evidence / (name + ".sbom-index.json")).read_text())
for platform in platforms:
    # A build for one platform records them unkeyed.
    single = len(platforms) == 1
    provenance = metadata.get("buildx.build.provenance/" + platform) or single and metadata.get("buildx.build.provenance")
    sbom = (sboms.get(platform) or (sboms if single else {})).get("SPDX")
    if not provenance or not sbom:
        sys.exit("no build provenance or SBOM for %s %s" % (name, platform))
    stem = "%s.%s" % (name, platform.replace("/", "-"))
    (evidence / (stem + ".provenance.json")).write_text(json.dumps(provenance, indent=1))
    (evidence / (stem + ".spdx.json")).write_text(json.dumps(sbom))
PY
    rm "$evidence/$name.sbom-index.json"
    printf '%s/%s:%s %s/%s@%s\n' "$prefix" "$name" "$tag" "$prefix" "$name" "$digest"
    continue
  fi
  write_provenance "$evidence/$name.metadata.json" "$evidence/$name.provenance.json"
  if [ -n "$push" ]; then
    # The attached SBOM, as the registry serves it.
    digest=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$evidence/$name.metadata.json")
    docker buildx imagetools inspect --builder "$builder" "$prefix/$name@$digest" --format '{{ json .SBOM.SPDX }}' > "$evidence/$name.spdx.json"
    printf '%s/%s:%s %s/%s@%s\n' "$prefix" "$name" "$tag" "$prefix" "$name" "$digest"
  else
    write_sbom "$prefix/$name:$tag" "$evidence/$name.spdx.json"
    printf '%s/%s:%s %s\n' "$prefix" "$name" "$tag" "$(docker image inspect --format '{{.Id}}' "$prefix/$name:$tag")"
  fi
done
echo "SBOMs and provenance in $evidence" >&2
