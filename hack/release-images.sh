#!/usr/bin/env bash
# Publish the release images for one semantic-release version.
# build.yml calls this from .releaserc.json in the publish step, before
# semantic-release-helm3 pushes the chart, so the tag the chart references
# already exists.
set -euo pipefail

version="${1:-}"
if [[ -z "${version}" ]]; then
  echo "usage: hack/release-images.sh <version>" >&2
  exit 1
fi

registry="${REGISTRY:-ghcr.io}"
owner="${OWNER:-polarpoint-io}"
# 1.7.1 -> 1.7. The floating minor tag is published with the release.
minor="${version%.*}"

build() {
  local name="$1"
  local context="$2"
  local file="$3"
  local image="${registry}/${owner}/${name}"

  echo "publishing ${image}:${version}"
  docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --push \
    --provenance=mode=max \
    --sbom=true \
    --tag "${image}:${version}" \
    --tag "${image}:${minor}" \
    --tag "${image}:latest" \
    --label "org.opencontainers.image.title=${name}" \
    --label "org.opencontainers.image.version=${version}" \
    --label "org.opencontainers.image.source=https://github.com/${owner}/agenthive" \
    --label "org.opencontainers.image.licenses=MIT" \
    --file "${file}" \
    "${context}"
}

build agenthive . Dockerfile
build agenthive-ui ui ui/Dockerfile
