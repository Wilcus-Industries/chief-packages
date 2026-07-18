# Uninstalling soul

This removes the soul *skill*. Your `data/memory/Soul.md` is the owner's data
(your evolving character) — **do not delete it** unless the owner asks; it is
git-backed in the memory store and can be re-adopted. Removing the skill does
not stop the prompt from inlining `Soul.md`: the session inlines it whenever the
file exists (via `chief.agent.prompt.read_soul`). To fully stop soul from
affecting the prompt, the owner must also move/rename `data/memory/Soul.md`.

1. Delete the installed skill dir `skills/soul/`.
2. Remove the `soul` entry from `data/installed.yaml`.
3. Leave `data/memory/Soul.md` in place unless the owner wants their character
   gone; if so, move it aside (the store's git history still holds it).
4. Delete this `UNINSTALL.md` from the cloned package
   (`data/packages/soul/UNINSTALL.md`) — its absence signals completion.
5. `restart` to bring the change live.
