#!/usr/bin/env bash
# Admin-CLI für die lokale Entwicklung, z. B.:  ./scripts/dev-admin.sh set-password
set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=dev-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/dev-env.sh"

cd "${BACKEND_DIR}"
exec uv run twinraid-admin "$@"
