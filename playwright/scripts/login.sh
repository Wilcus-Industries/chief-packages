#!/usr/bin/env bash
# Open chief's persistent browser profile headed so the owner can log in.
# Run from the chief repo root: bash skills/playwright/scripts/login.sh [url]
# Needs a desktop session (not plain SSH). Close the window when done.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
base="data/playwright"
exec node "$here/login.mjs" "$base/server" "$base/profile" "${1:-}"
