# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-only
# shellcheck disable=SC2034 # the images and image_labels are for the sourcing scripts
# Sourced by build-images.sh and CI. Every image
# build carries the org.opencontainers.image.* labels, and leaves its SBOM and
# build provenance as files; open-source tooling only: syft for the SBOM and
# BuildKit's SLSA provenance. Pushed images (build-images.sh --push) also carry
# both as attestations in the registry, next to the image.
# syft, run from its release image to write SPDX SBOMs of local images.
SYFT_IMAGE=anchore/syft:v1.54.1@sha256:3eb5379ba7b409c3f4069b686110527af0c47df993fa5c10d13e7cf34f49b1aa
# The BuildKit that pushes images with attestations (the docker driver may not
# support them), and its SBOM generator, which runs syft.
BUILDKIT_IMAGE=moby/buildkit:v0.34.0@sha256:b059f8d7226d0b326bc871af489a5dddf65e36af4d20d14251b072974d9ee87c
SBOM_GENERATOR=docker/buildkit-syft-scanner:1.12.0@sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9

# Sets image_labels to the --label arguments for one build of this checkout,
# and image_source to its source URL.
#   source    RT_IMAGE_SOURCE, else the GitHub Actions repository, else the
#             origin remote as an https URL without credentials
#   revision  the commit, with -dirty when the checkout has uncommitted changes
#   version   the version tag
#   created   the commit's time (UTC), so a rebuild of one commit labels alike
#   licenses  AGPL-3.0-only
set_image_labels() { # project-dir version
  local src revision created
  src=${RT_IMAGE_SOURCE:-}
  if [ -z "$src" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
    src=${GITHUB_SERVER_URL:-https://github.com}/$GITHUB_REPOSITORY
  fi
  if [ -z "$src" ]; then
    src=$(git -C "$1" remote get-url origin 2>/dev/null || true)
  fi
  src=$(printf '%s' "$src" | sed -E 's#^git@([^:]+):#https://\1/#; s#^(ssh|git)://([^@/]*@)?#https://#; s#^(https?://)[^@/]*@#\1#; s#\.git$##')
  case "$src" in
    https://*) ;;
    *) echo "image source unknown: set RT_IMAGE_SOURCE to the repository's https URL" >&2; return 1 ;;
  esac
  revision=$(git -C "$1" rev-parse HEAD)
  if [ -n "$(git -C "$1" status --porcelain)" ]; then revision=$revision-dirty; fi
  created=$(TZ=UTC git -C "$1" log -1 --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd)
  image_source=$src
  image_labels=(
    --label "org.opencontainers.image.source=$src"
    --label "org.opencontainers.image.revision=$revision"
    --label "org.opencontainers.image.version=$2"
    --label "org.opencontainers.image.created=$created"
    --label "org.opencontainers.image.licenses=AGPL-3.0-only"
  )
}

# Writes the SLSA provenance BuildKit recorded in a build's metadata file
# (built with BUILDX_METADATA_PROVENANCE=max) as JSON.
write_provenance() { # metadata-file out-file
  python3 -c 'import json, sys
provenance = json.load(open(sys.argv[1])).get("buildx.build.provenance")
if not provenance:
    sys.exit("no build provenance in " + sys.argv[1])
with open(sys.argv[2], "w") as out:
    json.dump(provenance, out, indent=1)' "$1" "$2"
}

# Writes an SPDX JSON SBOM of a local image with syft, from a saved copy of the
# image (so syft needs no access to the Docker daemon).
# The saved copy is removed whether or not the scan succeeds.
write_sbom() { # image-ref out-file
  local dir status=0
  dir=$(mktemp -d)
  docker save -o "$dir/image.tar" "$1" && chmod -R a+rX "$dir" &&
    docker run --rm -e SYFT_CHECK_FOR_APP_UPDATE=false -v "$dir:/in:ro" "$SYFT_IMAGE" \
      "docker-archive:/in/image.tar" --source-name "${1%:*}" --source-version "${1##*:}" \
      -o spdx-json > "$2.tmp" || status=$?
  rm -rf "$dir"
  if [ "$status" != 0 ]; then
    rm -f "$2.tmp"
    if [ "$status" = 137 ]; then
      echo "SBOM scan of $1 ran out of memory (killed, exit 137): give Docker more memory, or run parallel prepares one at a time" >&2
    fi
    return "$status"
  fi
  mv "$2.tmp" "$2"
}
