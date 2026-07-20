# Installing user

User gives you a `User.md` — the handful of standing facts about your owner
that are true on every turn (their name, age, where they live, what they do).
They are not memories: there is nothing to recall and nothing to search, so
they live in your prompt instead of a memory store. The package's `hooks.py`
registers a `pre_turn` hook that adds the file to every turn's prompt, so there
is **no prompt wiring to do**. It depends on the `memory` package: `User.md`
lives in that store (`data/memory/`) and is versioned by the same git repo.

1. Install `memory` first if it isn't already — follow `memory/INSTALL.md`.
2. Place the skill and seed a starter file: run
   `bash data/packages/user/install.sh` from your repo root. It copies the skill
   to `skills/user/` and seeds `data/memory/User.md` if you had none — an
   existing file is never overwritten. You can also do these by hand with your
   file tools.
3. Record it: add a `user: {source: cloned, commit: <this repo's HEAD>}` entry
   to `data/installed.yaml`. The hook only loads for a package listed here, so
   skipping this step leaves the skill installed and the facts absent from your
   prompt.
4. `restart` — the done-check runs, and the hook and skill go live.
5. Read the installed `skills/user/SKILL.md`, then read `data/memory/User.md`.
   The seed is placeholders; fill them in with your owner over the next few
   turns rather than guessing. The hook re-reads the file every turn, so every
   edit takes effect next turn — no restart needed for `User.md` edits.
