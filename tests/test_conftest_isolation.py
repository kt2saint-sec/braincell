# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_conftest_isolation.py — Pin the collection-time namespace isolation guard.

braincell/config.py snapshots BRAINCELL_DATA_NAMESPACE into the module constant
DATA_NAMESPACE at IMPORT time. This file deliberately imports braincell.config
at MODULE scope — exactly like test_native_shell.py / test_global.py — so the
import happens during pytest COLLECTION, before any fixture runs.

Without conftest.py's module-level `os.environ["BRAINCELL_DATA_NAMESPACE"] =
"braincell_test"` guard, this freezes the constant from the raw shell env
("braincell"), and every test whose helpers read the env at call time (e.g.
test_gui.py's _init_global_db vs gui.py's get_global_db_path) diverges —
the TestApiPool / TestApiStatus order-dependent flake.

This test FAILS if that guard is ever removed and PASSES with it in place,
regardless of file ordering.
"""

from braincell import config  # noqa: F401 — module-scope import is the point


def test_data_namespace_frozen_to_test_namespace_at_collection():
    """The import-time DATA_NAMESPACE snapshot must be the test namespace."""
    assert config.DATA_NAMESPACE == "braincell_test", (
        "braincell.config was imported (at collection time) without the "
        "conftest module-level BRAINCELL_DATA_NAMESPACE guard — the "
        "namespace-dependent GUI/global tests will order-flake."
    )
