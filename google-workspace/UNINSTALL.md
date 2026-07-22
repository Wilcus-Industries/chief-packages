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
4. Leave the `google_workspace_credentials` secret (the OAuth client) and the
   per-account tokens under `secrets/google_tokens/` in place unless the owner
   wants their Google access revoked. If they do: revoke the grant at
   <https://myaccount.google.com/permissions> (or per account via the token
   endpoint's revoke), then delete `secrets/google_tokens/<address>.json` for
   each account (and the `google_workspace_credentials` secret to drop the
   client entirely). These are owner data — never delete them unprompted.
5. Delete this `UNINSTALL.md` from the cloned package
   (`data/packages/google-workspace/UNINSTALL.md`) — its absence signals
   completion.
6. `restart` to bring the change live.
