# chief-packages

Capability packages for [chief](https://github.com/CrazyWillBear/chief), the
self-editing personal-agent daemon. chief clones this repo into
`data/packages/` the first time its `chief-pkg` CLI runs, and discovers these
packages alongside the ones bundled in core. Bundled packages win a name
collision.

## What a package is

One directory per package:

```
<name>/
  manifest.yaml     # name, description, and (informational) skills/config_keys/secrets
  INSTALL.md        # the document-driven install the agent follows
  UNINSTALL.md      # the document-driven uninstall; its last step deletes itself
  skills/<name>/    # SKILL.md(s) the install copies into the agent's skills/ dir
  install.sh        # optional convenience script the INSTALL.md may run via Bash
```

Install and uninstall are **document-driven**: the agent reads `INSTALL.md` /
`UNINSTALL.md` and carries out the steps with its own file tools
(`write_file`, `edit_file`) plus a guarded `restart`. There is no install tool
and no package manager daemon — `chief-pkg` only *discovers*
(`chief-pkg list` / `chief-pkg search <query>`, `--installed` to filter).

The last step of every `UNINSTALL.md` deletes the `UNINSTALL.md` itself; its
absence is the completion signal.

## Packages

- **memory** — a discipline over the agent's own file + git tools: a durable
  note store under `data/memory/` with a `MEMORY.md` index the agent recalls
  from each session.
- **soul** — gives the agent a `Soul.md` character file inlined at the top of
  its system prompt every turn. Depends on `memory` (they share the
  `data/memory/` store and its git history).
