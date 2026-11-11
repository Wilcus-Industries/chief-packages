# Installing playwright

Gives chief a real browser: the [Playwright MCP server](https://github.com/microsoft/playwright-mcp)
(`@playwright/mcp`, pinned in `scripts/playwright_tools.py`) over stdio,
driving installed Google Chrome headless on a **persistent chief-only
profile**, so the owner logs sites in once and chief stays logged in.
Depends on `screening`.

## Read before installing — tell the owner

- The profile at `data/playwright/profile` holds live session cookies for
  every site the owner logs it into. It is a credential: it stays under the
  gitignored `data/`, mode 700. Log it only into accounts chief should act in.
- Browsing is free; acting (click, type, submit, upload, page JS) raises an
  approval card per call until the owner answers "always" for that tool.
  `browser_run_code_unsafe` goes on `gate.never`.
- Navigation is not side-effect free — a GET can carry data out in a query
  string. Screening on everything the browser returns is the guard.

## Prerequisites

`node` + `npm` on PATH, and Google Chrome in `/Applications` (macOS). The
install checks both and fails loud.

## Steps

1. Install `screening` first if it isn't already.
2. Run `bash data/packages/playwright/install.sh` from the repo root with the
   `shell` tool. It:
   - npm-installs the pinned server into `data/playwright/server`;
   - creates `data/playwright/{profile,output,workspace}`;
   - copies the skill and its scripts to `skills/playwright/`;
   - wires `config.yaml` via `playwright_tools.py wire`: the
     `mcp_servers.playwright` entry (absolute paths, `cwd` confined to
     `data/playwright/workspace`, `PATH` with node's dir), the read tools onto
     `gate.approved`, `browser_run_code_unsafe` onto `gate.never`, and
     `mcp_playwright_*` onto `typesafe.screen_tools` **if** that list is set
     (absent = core's default, which already screens `mcp_*`). Lists are
     unioned, never replaced;
   - records the install (`chief.registry_apply`).
3. `restart`.
4. Verify: `uv run python -m chief.pkg.cli verify playwright` is clean, then
   `browser_navigate` to `https://example.com` and `browser_snapshot` shows
   "Example Domain". `browser_close` after.
5. Logins: tell the owner to run
   `bash skills/playwright/scripts/login.sh <url>` on the machine's desktop
   session, log in, and close the window (chief's browser must be closed
   first). Then confirm by navigating to the site.

## Bumping the pin

Change `PIN` in `scripts/playwright_tools.py`, list the new release's tools,
update `PINNED_TOOLS` and classify any new tool (the package tests fail until
you do), re-run `install.sh`, `restart`.
