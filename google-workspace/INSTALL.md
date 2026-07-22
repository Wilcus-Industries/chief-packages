# Installing google-workspace

Gmail, Calendar, and Drive for chief, via a pinned upstream Google Workspace
MCP server ([taylorwilsdon/google_workspace_mcp](https://github.com/taylorwilsdon/google_workspace_mcp),
PyPI `workspace-mcp`) launched over stdio. Reads are open; writes are gated.
Depends on the `screening` package for that gating.

## Install-time questions — ask both, explicitly

The install conversation must ask the owner these two things and record the
answers, so the flow is predictable and nothing is assumed silently:

1. **Assistant-signature on outbound mail?** Should mail this package sends on
   the owner's behalf carry a short assistant-signature disclosure (e.g. a
   trailer noting it was sent by the owner's assistant)? Default: **ask the
   owner**; do not add one silently. If yes, add the agreed trailer text to the
   `google_workspace` config and have the skill append it to every composed
   message before the send gate (the send still cards either way). If no, the
   owner is choosing to send unsigned — record that so it is a decision, not an
   omission.
2. **How does the deterministic version check run?** A **chief prompt-schedule**
   (portable) or an **OS-level timer** (fully LLM-free) — see step 8. Pick the
   one that fits the host; the check script is identical, only the wiring
   differs.

1. Run `bash data/packages/google-workspace/install.sh` with the `shell` tool.
   It copies the skill and the bundled `gw_aliases.py` to
   `skills/google-workspace/scripts/` (the path the skill documents), scaffolds
   an empty `data/google-workspace/aliases.txt` — edit that file to add your
   `name: address` account aliases — and seeds the inbound-Gmail screening
   classifier into `classifiers/gmail-inbound-screen.md` (never clobbered; pass
   `GATE_MODEL=<model-id>` to pin a guard model other than the default
   `openai/gpt-4.1-nano` — the value must be a plain model id, characters
   outside `[A-Za-z0-9._:/-]` are rejected). It then runs
   `scripts/setup_upstream.sh` (step 6): clones the **pinned** upstream MCP
   server to `data/google-workspace/server`, applies the auth-result-header
   patch, and warms its deps. That step **fails loud** (aborts the install) if
   the patch does not apply; set `GW_SKIP_WARM=1` to skip only the `uv sync`
   warm on an offline box.
2. Install `screening` first if it isn't already — writes route through it, and
   inbound Gmail content is screened before the model reads it. That screen is
   the manifest's `hooks:` block (`scripts/gmail_screen.py`): a `post_tool` hook
   that runs each Gmail content read through the seeded classifier at chief's
   `Annotate` seam — flagged mail is fenced with an untrusted marker (never
   dropped), a screener error fails safe to clean. It loads automatically once
   the package is recorded in `data/installed.yaml` (step 7). See the skill's
   "Screening inbound mail before the model reads it" section.
3. Owner consent + OAuth (consent by paste). First place the OAuth **client**:
   download a **Desktop app** OAuth client JSON from Google Cloud Console and save
   it as the `google_workspace_credentials` secret
   (`secrets/google_workspace_credentials`) — a **web** client is rejected, it
   needs the loopback redirect a Desktop client uses. This file holds the
   `client_id` + `client_secret`, and `secrets/` is often `0755`, so **tighten it**
   right after saving:
   ```
   chmod 600 secrets/google_workspace_credentials
   ```
   (`oauth_consent.py` also warns and re-tightens to `0600` on load if it finds
   the file group/other-readable.) Then authorize each account by paste — chief's
   host runs no callback listener, so consent comes back as a pasted redirect
   string, not a caught redirect:
   ```
   OC=skills/google-workspace/scripts/oauth_consent.py
   uv run python $OC url me@company.com          # consent URL to hand the owner
   uv run python $OC exchange me@company.com "<pasted redirect string>"
   ```
   The owner opens the URL on any device, consents, and pastes back the whole
   `http://localhost/?code=…` their browser lands on; `exchange` parses the code
   (a malformed paste is rejected), verifies the pasted `state` against the
   session `url` issued, exchanges it with the session's PKCE verifier, and — only
   after the returned `id_token`'s email claim matches the address — writes the
   credential **keyed by address** to `secrets/google_tokens/<address>.json` —
   owner-only (0600) in a 0700 dir, in the Google authorized-user shape the
   upstream server reads. A consent that authorized a **different** Google account
   is refused (anti-fixation), never stored under the requested address. A second
   account authorizes independently. Run `url` and `exchange` with the **same**
   `--credentials-dir` (default `secrets/google_tokens`), since `url` persists the
   session's state/PKCE there for `exchange` to check. **Coupling (#7):** the
   pinned `workspace-mcp` server reads per-account credentials from its own
   credentials directory; #7 points that directory at `secrets/google_tokens/`
   (or run consent with `--credentials-dir` set to wherever the server reads) so
   the server loads the same files consent writes. **Mock-debt (#8 → #10):** the
   exchange was built/tested against a fixture token endpoint, and the `id_token`
   email claim is decoded **without** JWKS signature verification; the live Google
   exchange and full JWKS signature verification land in #10.
4. Tool catalog + read/write split. The manifest command launches the server
   with `--tool-tier core --tools gmail drive calendar`, so it registers only
   the `core`-tier Gmail / Calendar / Drive tools — sixteen, each classified
   read vs. write in `skills/google-workspace/scripts/tool_gate.py`. (Without
   those flags upstream registers every service at every tier — ~60 tools — and
   the split below would not match what the server exposes.) **Reads**
   (search/list/get mail, events, and Drive files; the existing shareable link)
   have no side effect and auto-approve. **Writes** send mail, create/modify
   calendar events, or write to Drive/local disk — including
   `get_drive_file_download_url`, which lands file bytes on local disk in stdio
   mode — and always raise the gate's approval card (step 5). List the sets:
   ```
   TG=skills/google-workspace/scripts/tool_gate.py
   uv run python $TG reads          # read tool names to register (step 5)
   uv run python $TG writes         # write tool names — left OFF, they card
   ```
   Inbound sender trust (#4): the sender trust catalog
   (`google-workspace/scripts/sender_trust.py`) keys inbound mail on the
   verified auth-result sender — see the skill's "Inbound sender trust" section.
   Unknown verified senders route to owner-confirm; the write gate below is the
   control that still fires even for a catalogued sender.
5. Write gate — the read-by-name registration (security-load-bearing). Register
   the **read** tool names on chief's gate by-name approve set so they
   auto-approve, and leave every **write** tool OFF it so it raises the approval
   card. The approve set is `config.yaml`'s `gate.approved` list, which core
   folds into the live `GatePolicy` at boot. Add the read names to it:
   ```
   uv run python skills/google-workspace/scripts/tool_gate.py approved-yaml
   ```
   and merge that `gate.approved` block into `config.yaml` (keep any existing
   entries), then `restart`. **Write tools are deliberately never added.** Under
   chief's default-ask, empty-list posture a tool that is not on the approve set
   raises the card every time — so sending mail, creating/modifying events, and
   Drive/download writes all require an explicit owner "yes" until the owner
   decides otherwise. This is intentional and load-bearing: per the PRD the gate
   card is the one control that survives a clever injection. Do **not** add a
   write tool to `gate.approved` or replace this allowlist posture with a
   denylist — denial must stay explicit (a tool that nobody allowlisted must
   still card). **Never add a `"*"` entry to `gate.approved`**: core's
   `GatePolicy.decide` treats `"*"` as approve-all, so a single wildcard silently
   auto-approves every write and defeats the entire card posture. The heavier
   `screening` classification wiring is a separate concern (TODO(#5)); it layers
   on top of — it does not replace — this card.
6. Pin + patch + warm the upstream server (done by `install.sh`, which runs
   `scripts/setup_upstream.sh`). The exact release is pinned in
   `patches/upstream.pin` (tag **v1.22.0**, commit
   `6e1d1457746777f8512f52d40fb195b2a40bad36`) — never a moving branch, because
   the server holds a Gmail send credential. Setup clones that commit to
   `data/google-workspace/server`, verifies the checked-out HEAD equals the pin,
   applies `patches/0001-authentication-results-headers.patch`, and warms deps
   with `uv sync`. The manifest launch runs that patched checkout via `uv run
   --frozen --directory data/google-workspace/server workspace-mcp --tool-tier
   core --tools gmail drive calendar` — the pinned, warmed lockfile means the
   first stdio connect never races a dependency resolution.
   - **The patch is REQUIRED and the apply is fail-loud.** It adds the message's
     `Authentication-Results` header(s) to the Gmail read output. Without it the
     sender-trust catalog (#4, `scripts/sender_trust.py`) has nothing to verify,
     so it can never verify **any** sender — the catalog never falls back to the
     forgeable `From`; a missing AR header just fails closed and routes every
     message to owner-confirm, leaving the feature broken. That is why a
     non-applying patch is a HARD ERROR that aborts the install
     (`scripts/apply_upstream_patch.sh`), never a warning. Re-generate the patch
     if you bump the pin. The patch is offered upstream; once merged, delete it
     and bump the pin in the same change (setup then verifies the mechanism is
     already present in the tree and applies nothing — a lost patch file with the
     mechanism absent still hard-fails).
   - Do **not** drop the `--tool-tier` / `--tools` flags — they scope the surface
     to core-tier Gmail/Calendar/Drive (a bare launch registers every service at
     every tier, ~60 tools) and are what makes the read/write split above real.
   - The live MCP-over-stdio connection to a real Google account (which also
     exercises the OAuth grant, step 3) is validated in **#10**; this slice
     lands + proves the patch offline.
7. Record it: `uv run python -m chief.registry_apply google-workspace` adds the
   `google-workspace` entry to `data/installed.yaml` (the record the boot loader
   reads to load this package's hooks — the inbound-Gmail screen **and** the
   broken-auth amplifier, step 8). **The version stamp is the pin, not a
   separate field.** `patches/upstream.pin` (tag + commit) is the single source
   of truth for which upstream release is installed; the deterministic version
   check reads it directly, and uninstall reconciles against it — so there is no
   duplicate version to drift. Bumping the server means editing that pin (step
   6), never hand-editing `data/installed.yaml`.
8. **Broken authorization fails loud and stays down (default).** When a refresh
   token is revoked, consent expires, or a quota is exhausted, every Google tool
   must **error visibly and stay registered** — a capability that silently
   vanishes is one the owner keeps assuming they have. This is wired as a
   `post_tool` amplifier (`scripts/auth_failure.py`, registered alongside the
   inbound screen via the manifest `hooks:` block): a Google tool result carrying
   a broken-credential signal is annotated with a prominent, actionable re-auth
   notice, the original error kept below it. Nothing is dropped or deregistered.
   The verbosity is configurable, **loud by default**:
   ```yaml
   google_workspace:
     auth_failure_mode: loud   # loud (default) | quiet
   ```
   `quiet` is terser but never silent (an unrecognized value falls back to
   `loud`). Detecting a **live** revoked Google credential is exercised in #10;
   the mapping, the config switch, and the default are live now. On a broken-auth
   error, re-authorize the affected account with the consent-by-paste flow
   (step 3).
9. **Deterministic upstream version check (no model turn).** A newer upstream
   release should reach the owner as a notification, so the pin can be bumped
   deliberately (step 6) — the server holds a Gmail send credential, so nothing
   updates automatically. The **outcome** is `scripts/version_check.py check`:
   it reads `patches/upstream.pin`, asks GitHub for the repo's latest **stable**
   release, compares tags, and — only when a newer release exists — prints a
   notification to stdout and exits `10` (up to date → exit `0`, error → exit
   `2`). The comparison is purely deterministic; no LLM is in the path. **Wire it
   as a platform-chosen outcome, not a fixed recipe** (the install-time question
   above):
   - **OS-level timer (fully LLM-free — prefer where available).** A launchd
     `StartCalendarInterval` job, a systemd timer, or a `cron` entry runs
     `uv run python skills/google-workspace/scripts/version_check.py check` on a
     cadence (e.g. weekly) and, on exit `10`, pipes stdout to the platform
     notifier (`osascript`/`notify-send`) or an owner-watched log. No model ever
     runs.
   - **Chief prompt-schedule (portable).** With the `schedule` tool, create a
     prompt schedule (e.g. `spec: "0 14 * * 1"`) whose prompt is: *run the shell
     command `uv run python skills/google-workspace/scripts/version_check.py
     check` and, if it prints a notification, relay it to me verbatim.* The
     comparison still happens entirely in the script — the agent does no version
     reasoning, it only relays the verdict.

   **Verify the wiring works** before trusting it: run the check by hand now and
   confirm it exits `0` (you are up to date on the fresh pin):
   ```
   uv run python skills/google-workspace/scripts/version_check.py check
   ```
   To confirm it *would* notify on a newer release, point it at a saved fixture
   (any JSON with a higher `"tag_name"`) via `--releases-endpoint`, or copy the
   pin to a temp file with an older `UPSTREAM_TAG` and pass `--pin` — it should
   print the notification and exit `10`.
10. `restart` — the done-check runs, the hooks load, and the server goes live.
