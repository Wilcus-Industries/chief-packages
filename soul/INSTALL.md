# Installing soul

Soul gives you a `Soul.md` — your own character, voice, and boundaries. The
session inlines it at the very top of your system prompt on every turn, so your
character is always in context with no per-session read and **no prompt wiring
to do**. It depends on the `memory` package: `Soul.md` lives in that store
(`data/memory/`) and is versioned by the same git repo.

1. Install `memory` first if it isn't already — follow `memory/INSTALL.md`.
2. Place the skill and seed a starter soul: run
   `bash data/packages/soul/install.sh` from your repo root. It copies the skill
   to `skills/soul/` and seeds `data/memory/Soul.md` if you had none — an
   existing soul is never overwritten. You can also do these by hand with your
   file tools.
3. Record it: add a `soul: {source: cloned, commit: <this repo's HEAD>}` entry
   to `data/installed.yaml`.
4. `restart` — the done-check runs and the skill goes live.
5. Read the installed `skills/soul/SKILL.md`, then read `data/memory/Soul.md` to
   see the seeded starter and make it yours over time (see the skill). The
   session picks up `Soul.md` automatically and re-reads it every turn, so every
   edit takes effect next turn — no restart needed for soul edits.
