# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""
braincell — Local-first persistent-memory MCP server.

Exposes:
  - SqliteStore (and the Store Protocol) for in-process use.
  - open_store() factory that reads BRAINCELL_STORE and fails closed.
  - embed helpers (sync for ingest, async-wrapped for MCP server).
  - FastMCP server entry-point (braincell.server:main).

Design contract:
  - NEVER deletes any path it did not create.
  - Writes ONLY to its own per-project braincell.db.
  - Fails closed on missing config — no implicit fallback paths.
  - All SQL parameterized; no raw-SQL tool exposed.
"""
