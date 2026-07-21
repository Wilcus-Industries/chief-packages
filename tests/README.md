# tests

A test harness over the package manifests (see the top-level `README.md` for what
a package is). The load-bearing checks run each package's `mcp_servers` block
through chief core's *real* manifest loader (`chief.mcp_manifest`), reached by
putting a core checkout's `src/` on `sys.path` — see `conftest.py`.

## Core resolution knobs

`conftest.py` resolves a chief core checkout from, in order: `$CHIEF_CORE` (if
set, it is **authoritative** — a set-but-invalid path is a loud error, never a
silent fall-through), then `~/code/chief`, `~/chief-gf`, and a sibling `../chief`.

- **`CHIEF_CORE`** — pin an explicit core checkout. If it does not contain
  `src/chief/mcp_manifest.py`, the suite fails loud naming the bad path.
- **`CHIEF_CORE_REQUIRED=1`** — refuse to run unless a core checkout resolves.
  Without it, the real-loader tests *skip* when no core is found (so bare CI
  with no core checkout still passes). With it, an absent core is a hard error.

The owner's machines (dev + mini) carry a chief core checkout, so the done-check
on those machines should run with `CHIEF_CORE_REQUIRED=1` to guarantee the
real-loader tests actually execute instead of silently skipping:

```
CHIEF_CORE_REQUIRED=1 uv run pytest
```
