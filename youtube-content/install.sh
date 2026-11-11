#!/usr/bin/env bash
# Deterministic install for youtube-content: copy the skill + fetch script
# verbatim. The Python-dependency self-edit (youtube-transcript-api in
# pyproject) is NOT here — the agent does it from INSTALL.md.
#
# Sources come from this script's own dir (wherever the package is
# cloned); destination paths are relative to the repo root.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/skills/youtube-content"
dst="skills/youtube-content"
mkdir -p "$dst/scripts"
cp "$src/SKILL.md" "$dst/SKILL.md"
cp "$here/scripts/fetch_transcript.py" "$dst/scripts/"

# Record the install in the registry so discovery and the hooks loader see
# it — the one bookkeeping step that must never be left to hand-editing.
uv run python -m chief.registry_apply youtube-content --source cloned
