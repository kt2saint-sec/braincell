# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Karl Toussaint (kt2saint)
"""The embedding build gate: no more silent NULL-embedding first runs.

`_run_build` must refuse an unready embedder BEFORE minting a Project,
registering a path, or taking the mutation lock — offering a consented model
download only on an interactive terminal.
"""

from __future__ import annotations

import sys

import pytest

from braincell import cli, embed


def _status(**overrides):
    base = {
        "provider": "ollama", "model": "test-model:1b", "dim": 1024,
        "reachable": True, "model_present": True, "ok": True, "detail": "",
    }
    base.update(overrides)
    if "ok" not in overrides:
        base["ok"] = base["reachable"] and base["model_present"]
    return base


class TestRequireReadyEmbedder:
    def test_ready_embedder_passes_silently(self, monkeypatch, capsys):
        monkeypatch.setattr(embed, "embedder_status", lambda **_k: _status())
        cli._require_ready_embedder(offer_pull=True, context="build")
        assert capsys.readouterr().out == ""

    def test_non_tty_missing_model_fails_fast_with_remediation(self, monkeypatch):
        monkeypatch.setattr(
            embed, "embedder_status",
            lambda **_k: _status(model_present=False, detail="run: ollama pull test-model:1b"),
        )
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        with pytest.raises(SystemExit, match="ollama pull test-model:1b"):
            cli._require_ready_embedder(offer_pull=True, context="build")

    def test_unreachable_never_prompts_even_on_a_tty(self, monkeypatch):
        monkeypatch.setattr(
            embed, "embedder_status",
            lambda **_k: _status(reachable=False, model_present=False, detail="Ollama unreachable"),
        )
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(
            "builtins.input",
            lambda *_a: pytest.fail("prompted for a download while Ollama is unreachable"),
        )
        with pytest.raises(SystemExit, match="Ollama unreachable"):
            cli._require_ready_embedder(offer_pull=True, context="build")

    def test_interactive_consent_downloads_and_proceeds(self, monkeypatch):
        monkeypatch.setattr(
            embed, "embedder_status", lambda **_k: _status(model_present=False),
        )
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_a: "y")
        pulls = []

        def _ensure(*, pull, on_progress=None, **_k):
            pulls.append(pull)
            return _status()

        monkeypatch.setattr(embed, "ensure_embed_model", _ensure)
        cli._require_ready_embedder(offer_pull=True, context="build")
        assert pulls == [True]

    def test_interactive_decline_fails_fast_without_pull(self, monkeypatch):
        monkeypatch.setattr(
            embed, "embedder_status",
            lambda **_k: _status(model_present=False, detail="run: ollama pull test-model:1b"),
        )
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *_a: "n")
        monkeypatch.setattr(
            embed, "ensure_embed_model",
            lambda **_k: pytest.fail("pulled after an explicit decline"),
        )
        with pytest.raises(SystemExit, match="ollama pull"):
            cli._require_ready_embedder(offer_pull=True, context="build")


class TestBuildPathGating:
    def test_unready_build_leaves_no_side_effects(self, tmp_path, monkeypatch):
        """The gate fires before minting/registering — the regression is a
        refused build that has already created Project state."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(
            embed, "embedder_status",
            lambda **_k: _status(model_present=False, detail="not ready"),
        )
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        project = tmp_path / "project"
        project.mkdir()
        with pytest.raises(SystemExit, match="not ready"):
            cli._run_build(
                project, skip_transcripts=False, reembed=False, verbose=False,
            )
        leftovers = list((tmp_path / "xdg").rglob("*"))
        assert not leftovers, (
            f"a refused build minted or registered Project state: {leftovers}"
        )

    def test_skip_transcripts_build_never_probes_the_embedder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(
            embed, "embedder_status",
            lambda **_k: pytest.fail("skip-transcripts build probed the embedder"),
        )
        project = tmp_path / "project"
        project.mkdir()
        cli._run_build(
            project, skip_transcripts=True, reembed=False, verbose=False,
        )
