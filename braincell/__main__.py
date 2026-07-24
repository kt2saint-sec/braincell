# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""Package entry point: ``python -m braincell`` runs the FastMCP stdio server.

Equivalent to the console script ``braincell-mcp`` (see pyproject.toml). The
server resolves its brain from BRAINCELL_STORE + BRAINCELL_PROJECT_ID and fails
closed if neither an explicit path nor a project id is available.
"""

from braincell.server import main

if __name__ == "__main__":
    main()
