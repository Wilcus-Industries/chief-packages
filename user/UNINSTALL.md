# Uninstalling user

This removes the user *skill* and stops the hook. Your `data/memory/User.md` is
the owner's data — **do not delete it** unless the owner asks; it is git-backed
in the memory store and can be re-adopted.

1. Delete the installed skill dir `skills/user/`.
2. Remove the `user` entry from `data/installed.yaml`. This is what stops the
   hook: the boot loader only registers hooks for packages listed there, so
   once the entry is gone the facts no longer reach your prompt.
3. Leave `data/memory/User.md` in place unless the owner wants it gone; if so,
   move it aside (the store's git history still holds it).
4. Delete this `UNINSTALL.md` from the cloned package
   (`data/packages/user/UNINSTALL.md`) — its absence signals completion.
5. `restart` to bring the change live.
