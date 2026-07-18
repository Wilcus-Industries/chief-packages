# Uninstalling memory

This removes the memory *skill* and stops the discipline. Your actual notes in
`data/memory/` are the owner's data — **do not delete them** unless the owner
explicitly asks; they are git-backed and can be re-adopted later. Note `soul`
depends on this store; if `soul` is installed, uninstall it first (or keep the
store).

1. Delete the installed skill dir `skills/memory/`.
2. Remove the `memory` entry from `data/installed.yaml`.
3. Leave `data/memory/` (the store + its git history) in place unless the owner
   says otherwise.
4. Delete this `UNINSTALL.md` from the cloned package
   (`data/packages/memory/UNINSTALL.md`) — its absence signals completion.
5. `restart` to bring the change live.
