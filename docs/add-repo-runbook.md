# Adding a repo to braincell

This is the verified, copy-paste sequence for giving a new repository its own braincell
brain, wiring it into your MCP client, and (optionally) linking it to sibling projects so
cross-project recall works.

Prefer a guided flow? The Memory Map GUI has a one-click **✚ Add repo** wizard that walks
these same four steps for you — see [GUI users](#gui-users-the-add-repo-wizard) below.

## Prerequisites

- The embedder is reachable. By default that's a local Ollama model
  (`qwen3-embedding:4b` @ 1024-d) — `braincell build` will fail loudly if it can't reach it.
- The MCP client CLI you're wiring is on `PATH` (`claude`, `codex`, or `code`, matching
  `--client`).

## The 4-step sequence

```bash
# 1. Index the repo into its own brain (mints + registers a project ID if new)
braincell build /path/to/repo

# 2. Register the MCP server for this repo (default client: Claude Code)
braincell install --federate /path/to/repo   # or just: cd /path/to/repo && braincell install --federate

# 3. Group it with sibling repos for cross-project recall (repeat/extend as needed)
braincell family add <family-name> /path/to/repo /path/to/sibling …

# 4. Restart your MCP client so it loads the new server
```

Each step in detail:

1. **`braincell build /path/to/repo`** — resolves the path to a project ID (minting and
   registering one if this is a new repo) and ingests its agent transcripts into a
   dedicated SQLite brain at `~/.local/share/<namespace>/projects/<id>/braincell.db`. Safe
   to re-run (`braincell sync` is the incremental alias).
2. **`braincell install --federate`** — registers the braincell MCP server for this repo
   with your client (`claude mcp add` by default; `--client codex` / `--client vscode` for
   the others — those two get the MCP tools only, no hook). The `--federate` flag stamps
   `BRAINCELL_FEDERATE=on` into the server's launch environment, which is what lets a
   `recall`/`search` call with `scope='family'` fan out across sibling repos instead of
   raising (see the **FEDERATE-env note** below). Omit it if you only ever want
   single-project recall.
3. **`braincell family add <name> /path/to/repo /path/to/sibling …`** — families are
   **explicit**: there is no automatic grouping by directory name or prefix. A family is
   just a named list of paths; a member that isn't registered yet is skipped until it is
   (lazy-link), so it's safe to add a repo to a family before or after `braincell build`.
4. **Restart your MCP client** so it picks up the newly registered server.

## The fingerprint-match caveat

Family members only combine as real **vector** search if they were all built under the
same embedder — the same `BRAINCELL_EMBED_MODEL` / `BRAINCELL_EMBED_DIM`. A sibling built
with a different embedder degrades to a **keyword-only** contribution for that sibling
(never a crash, never a silently-wrong similarity score — braincell refuses to mix vector
spaces). If you want every family member to fully participate in semantic recall, build
them all with the same embedder settings. `BRAINCELL_FEDERATE_STRICT=on` drops
mismatched siblings entirely instead of degrading them, if you'd rather exclude them.

## The FEDERATE-env note (read this before filing a bug)

Without `--federate` at install time (or a hand-added `-e BRAINCELL_FEDERATE=on` on the
MCP registration), the `recall`/`search` **MCP tools** raise when called with
`scope='family'` in project mode — this is by design, not a bug: cross-project fan-out is
opt-in.

The **proactive family-recall hook is unaffected by this** — it always requests
federation for its own recall call regardless of your ambient environment, so arming it
with `braincell hook on` surfaces sibling-project memory automatically whether or not you
installed with `--federate`. The flag only gates the explicit `recall`/`search` MCP tool
calls (and the `braincell recall --scope family` CLI command run outside the hook).

## Arm proactive family recall

```bash
braincell hook on
```

This arms the (disarmed-by-default) `UserPromptSubmit` hook that injects a short "Family
memory" context block from sibling projects at the start of each turn. Turn it off any
time with `braincell hook off`, and check its state with `braincell hook status`.

## Verify it worked

```bash
# Confirm the family and its members resolved to real project IDs
braincell family ls

# Recall across the family from this repo (needs BRAINCELL_FEDERATE=on in the shell,
# since this runs the CLI directly rather than through the installed MCP server env —
# the MCP tool itself already carries the flag if you installed with --federate)
BRAINCELL_FEDERATE=on braincell recall "<a query that matches a sibling's note>" --scope family
```

A successful result includes a note whose content came from a sibling project's brain,
not just this one. If you didn't pass `--federate` at install time and try the same
`scope='family'` recall through the MCP tool (not the hook), expect it to raise
`scope='family' requires global mode. …` — that's the opt-in gate working as intended;
re-run `braincell install --federate` to enable it.

## GUI users: the Add project wizard

If you'd rather not type the four commands by hand, `braincell gui --allow-writes` (or
`braincell-map`) has a **✚ Add project** button that walks the exact same sequence — pick a
folder with the Qt system dialog or embedded folder navigator, build, install (with an
"Enable cross-project federation" checkbox, checked by default), and optionally add it to
a family — finishing with a reminder to restart your MCP client.

## Verified example

Run once on this box on 2026-07-08, against two throwaway repos under
`/tmp/.../scratchpad/addrepo-e2e/`, with an isolated data namespace, isolated
`XDG_DATA_HOME` (so the brains/path-registry/families.json land in scratch, not the real
store), and an isolated `BRAINCELL_CLAUDE_SETTINGS` (so the hook install writes to a
throwaway file, not the real `~/.claude/settings.json`), using the real local
`qwen3-embedding:4b` embedder (Ollama was up throughout). Both scratch dirs, the scratch
namespace, and the real `claude mcp` registration created below were removed after the
run — nothing here persists.

**Setup:**

```bash
export BRAINCELL_DATA_NAMESPACE=addrepo_probe
export XDG_DATA_HOME=/tmp/.../scratchpad/addrepo-e2e/xdg
export BRAINCELL_CLAUDE_SETTINGS=/tmp/.../scratchpad/addrepo-e2e/claude-settings.json
mkdir -p /tmp/.../scratchpad/addrepo-e2e/repoA /tmp/.../scratchpad/addrepo-e2e/repoB
```

**Steps run, verbatim, with actual output:**

```
$ braincell build .../repoA
BrainCell build for project 01KX0BPKZ781RD3318KJYK9FC6 (.../repoA)
  Ingesting agent transcripts...
  Transcript ingest: 0 ingested, 0 skipped, 0 failed, 4162 unattributed, 0 out-of-family, 0 chunks, 0 secret-rejections, 0 skill-docs created.
BrainCell build complete.

$ braincell build .../repoB
BrainCell build for project 01KX0BPPDAZ8MH2FFRYQWQJ1VB (.../repoB)
  Ingesting agent transcripts...
  Transcript ingest: 0 ingested, 0 skipped, 0 failed, 4162 unattributed, 0 out-of-family, 0 chunks, 0 secret-rejections, 0 skill-docs created.
BrainCell build complete.

$ braincell family add probefam .../repoA .../repoB
Family 'probefam' (2 member(s)):
  .../repoA
  .../repoB

$ braincell family ls
[probefam] (2 member(s))
  .../repoA  -> 01KX0BPKZ781RD3318KJYK9FC6
  .../repoB  -> 01KX0BPPDAZ8MH2FFRYQWQJ1VB

$ braincell install --federate .../repoA
✓ registered braincell MCP with claude-code (project 01KX0BPKZ781RD3318KJYK9FC6) → ~/braincell/.venv/bin/python -m braincell.server  [federation: ON]
✓ installed family-recall hook (DISARMED)

Next steps:
  1. Restart Claude Code so it loads the new MCP server.
  2. `braincell hook on`  — arm proactive family memory (needs a family;
     see `braincell family add`). Turn off any time with `braincell hook off`.
```

(The transcript ingest step shows 0 ingested/0 chunks because a fresh throwaway repo has
no prior agent transcripts — expected, and it doesn't block registration. A memory note
describing a blue-green/canary deploy pipeline was seeded directly into repoB's brain
with a real embedding so there was something sibling-specific to recall — the CLI
intentionally has no `remember` subcommand, since that tool is MCP-only, so this used the
same `store.remember()` the MCP tool calls, one layer down.)

**Negative control** — `scope='family'` recall from repoA *without* `BRAINCELL_FEDERATE`
set, proving the opt-in gate is real:

```
$ braincell recall "blue-green canary rollout percentage" --path .../repoA --scope family
braincell recall: scope='family' requires global mode. Set BRAINCELL_MODE=global and open a global brain, or use scope='self' (the default) for this project's brain.
(exit code 2)
```

**Positive — the actual sibling-note recall**, same query, same repo, with
`BRAINCELL_FEDERATE=on`:

```
$ BRAINCELL_FEDERATE=on braincell recall "blue-green canary rollout percentage" --path .../repoA --scope family
[note] The deploy pipeline uses a blue-green rollout with a 5% canary held for 10 minutes before full cutover.
```

That note lives only in repoB's brain — repoA has zero memory notes of its own — so this
is a real sibling-project hit via federation, not a coincidence of scope='self'.

Confirms the FEDERATE-env note above: the flag is what gates the MCP-tool/CLI path
(negative control), and turning it on makes the sibling note reachable (positive result).
The `claude mcp add` call in step 2 really does register with the local `claude` CLI (not
just write to the isolated settings file — that override only covers the hook install);
it was removed again immediately after this run via `claude mcp remove braincell -s local`
from `.../repoA` so no throwaway registration was left behind.
