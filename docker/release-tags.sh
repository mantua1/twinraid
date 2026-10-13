#!/usr/bin/env bash
# Berechnet die Image-Tags für eine Veröffentlichung (Konzept 5.8), einen pro Zeile.
#
# Aufruf: docker/release-tags.sh <git-ref> <commit-sha> <version-im-code>
#   refs/heads/main          → edge, sha-<7 Zeichen>
#   refs/tags/v0.3.0         → 0.3.0, 0.3, latest   (ab 1.0 zusätzlich die Hauptversion)
#   refs/tags/v0.3.0-rc.1    → 0.3.0-rc.1
# Bricht ab, wenn der Tag keine Version ist oder nicht zur Version im Code passt.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <git-ref> <commit-sha> <version>" >&2
  exit 2
fi
ref="$1"
sha="$2"
version="$3"
semver='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$'

case "${ref}" in
  refs/heads/main)
    if [[ ! "${sha}" =~ ^[0-9a-f]{40}$ ]]; then
      echo "invalid commit sha '${sha}'" >&2
      exit 1
    fi
    echo "edge"
    echo "sha-${sha:0:7}"
    ;;
  refs/tags/v*)
    tag="${ref#refs/tags/v}"
    if [[ ! "${tag}" =~ ${semver} ]]; then
      echo "tag v${tag} is not a version like v1.2.3 or v1.2.3-rc.1" >&2
      exit 1
    fi
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    prerelease="${BASH_REMATCH[4]}"
    if [[ "${tag}" != "${version}" ]]; then
      echo "tag v${tag} does not match the version ${version} in the code" >&2
      exit 1
    fi
    echo "${tag}"
    if [[ -z "${prerelease}" ]]; then
      echo "${major}.${minor}"
      if [[ "${major}" != "0" ]]; then
        echo "${major}"
      fi
      echo "latest"
    fi
    ;;
  *)
    echo "refusing to publish from ${ref}; only main and version tags are published" >&2
    exit 1
    ;;
esac
