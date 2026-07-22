# Uninstalling google-workspace

This stops the Google Workspace MCP server from launching, unwires its
write-gate posture and hooks, and deletes the package's local data **including
the owner's Google credentials**. Google access is only fully revoked when the
owner also revokes the grant on Google's side (step 6) — this uninstall cannot
do that for them, and a half-completed revoke is worse than a clear instruction,
so it deletes the local tokens and tells the owner exactly how to finish.

1. Delete the installed skill dir `skills/google-workspace/` (this also removes
   the copied helper scripts `gw_aliases.py`, `tool_gate.py`, `oauth_consent.py`,
   and `version_check.py`).
2. Unwire the MCP server + its gate/hook posture:
   - Remove the read tool names this package added to `config.yaml`'s
     `gate.approved` list (the block `tool_gate.py approved-yaml` printed at
     install — the `mcp_google-workspace_*` read names). Leave any unrelated
     entries. With the server gone these names resolve to nothing, but leaving
     them is stale config.
   - Deregister the package: `uv run python -m chief.registry_apply
     google-workspace --remove`. This drops the `google-workspace` entry from
     `data/installed.yaml`, so the boot loader stops loading its `post_tool`
     hooks — the inbound-Gmail screen **and** the broken-auth amplifier — and the
     manifest's MCP server stops launching.
   - The inbound-Gmail screening classifier def
     `classifiers/gmail-inbound-screen.md` is owner-tunable content; leave it
     unless the owner wants it gone (deleting it affects only this now-removed
     screen).
3. Delete the cloned upstream server checkout `data/google-workspace/server`
   (the pinned + patched `workspace-mcp` tree install landed) and the package's
   local data dir `data/google-workspace/` (the `aliases.txt` account aliases
   live here — owner data, but tied to this package; delete with the rest unless
   the owner asks to keep the alias list).
4. **Delete the local Google credentials.** These tokens can send mail as the
   owner, so removing the package must not leave them lying in the secret store:
   - `secrets/google_tokens/` — every per-account `<address>.json` credential
     **and** any leftover `.<address>.pending.json` consent-session files.
     Delete the whole directory.
   - `secrets/google_workspace_credentials` — the OAuth **client** JSON (the
     desktop-app id/secret). Delete it too, to drop the client entirely.
5. Remove the `google_workspace` config block from `config.yaml` if present
   (e.g. `trusted_authserv_id`, `auth_failure_mode`) — nothing reads it once the
   package is gone.
6. **Revoke the grant on Google's side (owner action — do this, deleting the
   local tokens is not enough).** Deleting `secrets/google_tokens/` stops *this*
   host from using the credentials, but the OAuth grant still exists at Google
   until the owner revokes it. For each authorized account, sign in and remove
   the app's access at <https://myaccount.google.com/permissions> (or revoke a
   specific token via Google's token-revocation endpoint). Until the owner does
   this, the grant remains revocable but live at Google — so state it plainly and
   confirm they have done it. This uninstall deliberately does **not** revoke
   automatically: a partial/failed programmatic revoke would leave the owner
   unsure of the real state, and a clear instruction they complete is safer.
7. `restart` to bring the change live (the server no longer launches; the hooks
   no longer load).
8. Delete this `UNINSTALL.md` from the cloned package
   (`data/packages/google-workspace/UNINSTALL.md`) — its absence signals the
   uninstall completed.
