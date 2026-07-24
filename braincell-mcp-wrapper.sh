#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
# braincell-mcp-wrapper.sh — portable launcher for the BrainCell MCP server.
#
# No hardcoded paths or project IDs. Configure entirely via env (all optional,
# with sane local-first defaults):
#
#   BRAINCELL_STORE           must be 'sqlite' (default).
#   BRAINCELL_PROJECT_ID      the project ULID to serve. Required by the server
#                             (it fails closed if unset). Run `braincell register
#                             <path>` once to mint it, then export it here or in
#                             your MCP client's server config.
#   BRAINCELL_EMBED_PROVIDER  'ollama' (default, local, $0) | 'openai' (hosted).
#   BRAINCELL_EMBED_MODEL     override the embedder (default: bge-m3 for ollama).
#                             Must match the model the brain was
#                             built with, or the fingerprint gate refuses to open.
#   BRAINCELL_PYTHON          interpreter to run with. Default: use the installed
#                             `braincell-mcp` console script (no interpreter pin).
#
# The OpenAI provider needs OPENAI_API_KEY in the environment; the local ollama
# default needs only a running ollama daemon.
set -euo pipefail

# Defaults (non-secret) — override any of these in the environment.
export BRAINCELL_STORE="${BRAINCELL_STORE:-sqlite}"
export BRAINCELL_EMBED_PROVIDER="${BRAINCELL_EMBED_PROVIDER:-ollama}"

# Prefer the installed console script (pip/uv install). If BRAINCELL_PYTHON is
# set, run the package as a module with that interpreter instead.
if [ -n "${BRAINCELL_PYTHON:-}" ]; then
  exec "$BRAINCELL_PYTHON" -m braincell "$@"
fi
exec braincell-mcp "$@"
