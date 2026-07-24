# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
#
# Portable BrainCell Map launcher (Windows / PowerShell).
#
# Prefers the installed `braincell-map` console script; falls back to a python
# that can import braincell. Extra args (e.g. --port 9000) are forwarded.

$ErrorActionPreference = "Stop"

$mapCmd = Get-Command braincell-map -ErrorAction SilentlyContinue
if ($mapCmd) {
    Start-Process -FilePath $mapCmd.Source -ArgumentList $args
    exit 0
}

foreach ($py in @("python", "python3", "py")) {
    $pyCmd = Get-Command $py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        & $pyCmd.Source -c "import braincell" 2>$null
        if ($LASTEXITCODE -eq 0) {
            & $pyCmd.Source -c "import sys; from braincell.cli import main_map; main_map(sys.argv[1:])" @args
            exit $LASTEXITCODE
        }
    }
}

Write-Error "braincell-map: could not find the 'braincell-map' script or a python that can import braincell. Install with: pip install braincell-mcp[gui]"
exit 1
