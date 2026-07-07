#!/usr/bin/env bash
# Copy the local-model compose + LiteLLM config into the ~/oss-crs checkout.
# OSS-CRS resolves the litellm config_path relative to its own root, so the
# files must live there; this repo keeps the canonical copies. Re-run after
# editing either file.
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${OSS_CRS_DIR:-$HOME/oss-crs}/example/crs-claude-code"
[ -d "$DEST" ] || { echo "error: $DEST not found (is ~/oss-crs cloned?)" >&2; exit 1; }
cp -v "$SRC_DIR/compose-local.yaml" "$SRC_DIR/litellm-config-local.yaml" "$DEST/"
