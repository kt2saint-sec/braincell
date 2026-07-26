# Contributing to braincell-mcp

Thanks for your interest. A few things to know before you open a pull request — the most
important being the **Contributor License grant**, which keeps this project's dual-license
model intact.

> **Not legal advice.** This file is a practical template for a small/solo project. If
> contribution volume grows, automate the sign-off with a CLA bot (e.g. CLA Assistant) and
> have a lawyer review this text before relying on it for commercial relicensing.

---

## Why a CLA (read this first)

`braincell-mcp` is **dual-licensed**: AGPL-3.0-or-later for everyone, plus a separate
**commercial license** the copyright holder sells for proprietary/closed-source use (see
`LICENSE` and `COMMERCIAL-LICENSE.md`).

For that to work, the maintainer must be able to license **every** line — including yours —
under **both** the AGPL and a commercial license. A plain open-source contribution doesn't
grant that commercial-relicensing right. So contributions require the grant below.

You **keep the copyright** to your work. You're granting a license, not signing it away.

---

## Contributor License grant

By submitting a Contribution (a pull request, patch, or any code/docs you propose for
inclusion), you agree to both of the following:

### 1. License grant

> You grant **Karl Toussaint (kt2saint)** (“the Maintainer”) a perpetual, worldwide,
> non-exclusive, royalty-free, irrevocable license to use, reproduce, modify, prepare
> derivative works of, publicly display, publicly perform, sublicense, and distribute your
> Contribution and derivative works of it, **and to relicense the Contribution under any
> terms, including the GNU AGPL-3.0-or-later and proprietary/commercial licenses.**
>
> You retain copyright in your Contribution. You represent that each Contribution is your
> original work, or that you have the right to submit it under this grant, and that it does
> not knowingly infringe any third party's rights.

### 2. Developer Certificate of Origin (sign-off)

Every commit must be signed off with `git commit -s`, which appends:

    Signed-off-by: Your Name <your-email@example.com>

That sign-off certifies the standard **Developer Certificate of Origin 1.1**:

```
Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

No sign-off + no agreement to the §1 grant → the contribution cannot be merged (it would
break the dual-license). PRs without a sign-off will be asked to amend.

---

## How to contribute

1. Fork and branch from `main` (`feat/...`, `fix/...`, `docs/...`).
2. Make focused changes — one concern per PR.
3. **New source files must carry the SPDX header** (keeps licensing airtight):
   ```
   # SPDX-License-Identifier: AGPL-3.0-or-later
   # Copyright (c) 2026 Karl Toussaint (kt2saint)
   ```
   Do not add your own copyright line; per the §1 grant the project licenses your work.
4. Sign off your commits: `git commit -s`.
5. Open the PR with a clear description of what and why.

## Dev setup & checks

The base package includes the native GUI runtime (PySide6/QtWebEngine). To work on it:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,openai]"

# sanity
python -c "import braincell.server, braincell.cli; print('ok')"
python -m braincell.cli --help
ruff check braincell
python -m pytest
```

The Memory Map is a native desktop application backed internally by the existing
localhost-only FastAPI/uvicorn transport and embedded SPA. Changes must preserve the
window-owned lifecycle: `braincell start`, `braincell gui`, and `braincell-map` create or
activate a native window, and closing it stops the server. Do not add an external-viewer or
headless-GUI fallback, an always-on GUI service, or optionalize PySide6.

## Scope

`braincell-mcp` is the **Project-memory serving and Build** layer
(Search/Recall/Remember + Build/sync). It
indexes and ranks content you feed it — it does not generate content itself (no parsing,
clustering, or summarization pipeline). Keep the package self-contained — no new hard
dependency on an external content-generation tool.
