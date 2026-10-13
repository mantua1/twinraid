#!/usr/bin/env bash
# Prüft ohne Anmeldung, dass ein Image öffentlich abrufbar ist und den erwarteten
# Digest hat (Konzept 5.8). Läuft im Veröffentlichungs-Job nach dem Push.
#
# Aufruf: docker/check-public-image.sh <registry/name> <tag> <sha256:digest>
# Beispiel: docker/check-public-image.sh ghcr.io/mantua1/twinraid 0.3.0 sha256:…
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <registry/name> <tag> <sha256:digest>" >&2
  exit 2
fi
repository="$1"
tag="$2"
expected="$3"
registry="${repository%%/*}"
name="${repository#*/}"
accept="application/vnd.oci.image.manifest.v1+json,application/vnd.oci.image.index.v1+json"
accept="${accept},application/vnd.docker.distribution.manifest.v2+json"
accept="${accept},application/vnd.docker.distribution.manifest.list.v2+json"

not_public() {
  cat >&2 <<MSG
The image ${repository}:${tag} cannot be pulled without login.
New packages on ghcr.io are private. Make the package public once:
  GitHub → your profile → Packages → ${name##*/} → Package settings
  → Danger Zone → Change visibility → Public.
Then re-run this job ("Re-run failed jobs").
MSG
  exit 1
}

digest=""
for attempt in 1 2 3 4 5; do
  # Anonymes Token: funktioniert nur für öffentliche Pakete.
  if token="$(curl -fsS "https://${registry}/token?scope=repository:${name}:pull" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"; then
    digest="$(curl -fsS -o /dev/null -D - -H "Authorization: Bearer ${token}" -H "Accept: ${accept}" \
      "https://${registry}/v2/${name}/manifests/${tag}" \
      | tr -d '\r' | awk -F': ' 'tolower($1) == "docker-content-digest" { print $2 }' || true)"
  fi
  [[ -n "${digest}" ]] && break
  echo "attempt ${attempt}: not pullable yet, waiting" >&2
  sleep 5
done

[[ -n "${digest}" ]] || not_public
if [[ "${digest}" != "${expected}" ]]; then
  echo "${repository}:${tag} has digest ${digest}, expected ${expected}" >&2
  exit 1
fi
echo "ok: ${repository}:${tag} is public with digest ${digest}"
