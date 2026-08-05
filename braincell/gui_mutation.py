# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""One event-loop-local mutation lease shared by every GUI write surface."""

from __future__ import annotations


class GuiMutationBusy(RuntimeError):
    """A GUI mutation is already active."""


class GuiMutationCoordinator:
    """Non-blocking ownership gate for ingest, maintenance, clear, and undo."""

    def __init__(self) -> None:
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        return self._owner

    def claim(self, owner: str) -> None:
        if self._owner is not None:
            raise GuiMutationBusy(
                f"{owner} refused: GUI mutation {self._owner!r} is already running."
            )
        self._owner = owner

    def release(self, owner: str) -> None:
        if self._owner == owner:
            self._owner = None
