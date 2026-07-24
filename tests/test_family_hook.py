# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_family_hook.py — the packaged UserPromptSubmit hook (braincell/family_hook.py).

Offline: the `braincell recall` subprocess is monkeypatched. Asserts the opt-in gate
(disarmed → {}), the injected "Family memory" block when armed, and fail-quiet on a
non-zero recall / empty result.
"""

from __future__ import annotations

import io
import json
import subprocess

from braincell import family_hook as fh


def _run_hook(monkeypatch, payload: dict):
    """Drive fh.main() with a payload. The no-op paths sys.exit(0); the happy path
    returns normally — tolerate both, but never a non-zero exit."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    try:
        fh.main()
    except SystemExit as exc:
        assert exc.code in (0, None)


def test_disarmed_is_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRAINCELL_FAMILY_HOOK_FLAG", str(tmp_path / "absent.txt"))
    _run_hook(monkeypatch, {"prompt": "hello", "cwd": str(tmp_path)})
    assert capsys.readouterr().out.strip() == "{}"


def _arm(tmp_path, monkeypatch):
    flag = tmp_path / "flag.txt"
    flag.write_text("armed")
    monkeypatch.setenv("BRAINCELL_FAMILY_HOOK_FLAG", str(flag))


def test_armed_injects_family_block(tmp_path, monkeypatch, capsys):
    _arm(tmp_path, monkeypatch)
    notes = [{"kind": "decision", "content": "use bge-m3", "project_id": "01ABCDEFGH"}]

    def fake_run(argv, **k):
        assert "recall" in argv and "--scope" in argv and "family" in argv
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(notes), stderr="")

    monkeypatch.setattr(fh.subprocess, "run", fake_run)
    _run_hook(monkeypatch, {"prompt": "which embedder?", "cwd": str(tmp_path)})

    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Family memory" in ctx and "use bge-m3" in ctx and "[decision]" in ctx


def test_armed_but_recall_fails_is_quiet(tmp_path, monkeypatch, capsys):
    _arm(tmp_path, monkeypatch)
    monkeypatch.setattr(fh.subprocess, "run",
                        lambda argv, **k: subprocess.CompletedProcess(argv, 1, stdout="", stderr="err"))
    _run_hook(monkeypatch, {"prompt": "q", "cwd": str(tmp_path)})
    assert capsys.readouterr().out.strip() == "{}"


def test_armed_empty_result_is_quiet(tmp_path, monkeypatch, capsys):
    _arm(tmp_path, monkeypatch)
    monkeypatch.setattr(fh.subprocess, "run",
                        lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout="[]", stderr=""))
    _run_hook(monkeypatch, {"prompt": "q", "cwd": str(tmp_path)})
    assert capsys.readouterr().out.strip() == "{}"


def test_armed_empty_prompt_is_noop(tmp_path, monkeypatch, capsys):
    _arm(tmp_path, monkeypatch)
    _run_hook(monkeypatch, {"prompt": "   ", "cwd": str(tmp_path)})
    assert capsys.readouterr().out.strip() == "{}"
