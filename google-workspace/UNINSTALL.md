# Uninstalling google-workspace

> **Stub.** Placeholder shape with `TODO(#N)` pointers; fleshed out alongside the
> install slices.

This removes the google-workspace skill and stops the MCP server from launching.
The owner's Google credentials are owner data — **do not delete them** unless the
owner asks.

1. Delete the installed skill dir `skills/google-workspace/`.
2. Remove the `google-workspace` MCP server + its `screening` write-gate wiring.
   TODO(#6): reference the exact gate config to unwind.
3. Remove the `google-workspace` entry from `data/installed.yaml`.
   TODO(#9): reconcile with the version stamp the install wrote.
4. Leave the `google_workspace_credentials` secret in place unless the owner
   wants their Google access revoked. TODO(#8): document revoking the OAuth grant
   if so.
5. Delete this `UNINSTALL.md` from the cloned package
   (`data/packages/google-workspace/UNINSTALL.md`) — its absence signals
   completion.
6. `restart` to bring the change live.
