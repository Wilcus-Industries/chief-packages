# Installing memory

Memory is a discipline over your own file and git tools, not new code — one
skill plus an empty store.

1. Place the skill and seed the store: run `bash memory/install.sh` via Bash
   (paths inside it are relative to the repo root, so run it from your repo
   root with the package dir on the path — e.g.
   `bash data/packages/memory/install.sh`). It copies the skill to
   `skills/memory/` and creates `data/memory/` with a `MEMORY.md` index and a
   `facts/` dir, git-initialised — an existing store is never overwritten. You
   can also do these steps by hand with your file tools.
2. Record it: add a `memory: {source: cloned, commit: <this repo's HEAD>}`
   entry to `data/installed.yaml`.
3. `restart` — the done-check runs and the skill goes live.
4. Read the installed `skills/memory/SKILL.md` once so the recall/save/index
   rules are fresh this session.

From here follow the skill: recall before you act, save durable facts as you
learn them, and keep `MEMORY.md` pointing at them.
