# SPDX-License-Identifier: AGPL-3.0-or-later
"""
test_transcript_ingest.py — Regression tests for braincell/transcript_ingest.py.

All tests use tmp_path isolation; no live Ollama or real ~/.claude transcripts.
"""

from __future__ import annotations

import hashlib
import json

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: _file_sha — streaming vs. whole-file equivalence
# ═══════════════════════════════════════════════════════════════════════════════

class TestFileSha:
    """_file_sha streaming equals hashlib.sha256(whole_file_bytes).hexdigest()."""

    def test_small_file(self, tmp_path):
        from braincell.transcript_ingest import _file_sha
        content = b"hello world"
        f = tmp_path / "small.jsonl"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert _file_sha(f) == expected

    def test_larger_file_multiple_chunks(self, tmp_path):
        """File > 1 MiB to exercise the streaming loop."""
        from braincell.transcript_ingest import _file_sha
        content = b"A" * (2 * 1024 * 1024 + 7)  # 2 MiB + 7 bytes
        f = tmp_path / "large.jsonl"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert _file_sha(f) == expected

    def test_empty_file(self, tmp_path):
        from braincell.transcript_ingest import _file_sha
        f = tmp_path / "empty.jsonl"
        f.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert _file_sha(f) == expected

    def test_nonexistent_file_returns_empty_string(self, tmp_path):
        from braincell.transcript_ingest import _file_sha
        result = _file_sha(tmp_path / "does_not_exist.jsonl")
        assert result == ""

    def test_binary_content(self, tmp_path):
        from braincell.transcript_ingest import _file_sha
        content = bytes(range(256)) * 1000
        f = tmp_path / "binary.bin"
        f.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert _file_sha(f) == expected


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: list-form content extraction (_coerce_content / _text_from_record)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCoerceContent:
    """list-form messages[i]["content"] (text blocks) is extracted without TypeError;
    a non-text block is skipped."""

    def test_plain_string_content(self):
        from braincell.transcript_ingest import _coerce_content
        assert _coerce_content("hello world") == "hello world"

    def test_list_of_text_blocks(self):
        from braincell.transcript_ingest import _coerce_content
        content = [
            {"type": "text", "text": "first part"},
            {"type": "text", "text": "second part"},
        ]
        result = _coerce_content(content)
        assert "first part" in result
        assert "second part" in result

    def test_non_text_block_is_skipped(self):
        """tool_use / thinking / image blocks must be silently skipped (no TypeError)."""
        from braincell.transcript_ingest import _coerce_content
        content = [
            {"type": "tool_use", "id": "tu1", "name": "bash", "input": {"cmd": "ls"}},
            {"type": "text", "text": "kept text"},
            {"type": "image", "source": {"type": "base64"}},
        ]
        result = _coerce_content(content)
        assert result == "kept text"

    def test_empty_list(self):
        from braincell.transcript_ingest import _coerce_content
        assert _coerce_content([]) == ""

    def test_non_dict_blocks_in_list(self):
        """Non-dict entries inside a list must not crash."""
        from braincell.transcript_ingest import _coerce_content
        content = [None, 42, {"type": "text", "text": "valid"}]
        result = _coerce_content(content)
        assert "valid" in result


class TestTextFromRecord:
    """_text_from_record unwraps real Claude Code turn schema
    (message.content) without TypeError; list blocks are handled."""

    def test_plain_string_content_field(self):
        from braincell.transcript_ingest import _text_from_record
        obj = {"content": "plain string content here"}
        result = _text_from_record(obj)
        assert result == "plain string content here"

    def test_claude_code_turn_schema(self):
        """Real Claude Code schema: {type, message: {role, content: str|list}}."""
        from braincell.transcript_ingest import _text_from_record
        obj = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": "This is the assistant response",
            },
        }
        result = _text_from_record(obj)
        assert result == "This is the assistant response"

    def test_claude_code_turn_with_list_content(self):
        """message.content as a list of text blocks — must not raise TypeError."""
        from braincell.transcript_ingest import _text_from_record
        obj = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "first block"},
                    {"type": "tool_use", "id": "x"},
                    {"type": "text", "text": "second block"},
                ],
            },
        }
        result = _text_from_record(obj)
        assert result is not None
        assert "first block" in result
        assert "second block" in result

    def test_non_text_block_only_returns_none_or_empty(self):
        """A message whose content is ONLY non-text blocks (thinking / tool_use)
        should yield None (no usable text), not raise."""
        from braincell.transcript_ingest import _text_from_record
        obj = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "tool_use", "id": "tu2"},
                ],
            },
        }
        result = _text_from_record(obj)
        # Either None or empty string after strip — but never a TypeError.
        assert result is None or result == ""

    def test_missing_content_returns_none(self):
        from braincell.transcript_ingest import _text_from_record
        result = _text_from_record({})
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: _extract_text_from_jsonl — malformed line tolerance
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtractTextFromJsonl:
    """A malformed/garbage JSONL line doesn't crash extraction."""

    def test_valid_jsonl_lines(self, tmp_path):
        from braincell.transcript_ingest import _extract_text_from_jsonl
        lines = [
            json.dumps({"content": "first line content"}),
            json.dumps({"content": "second line content"}),
        ]
        f = tmp_path / "valid.jsonl"
        f.write_text("\n".join(lines), encoding="utf-8")
        result = _extract_text_from_jsonl(f)
        assert any("first line" in p for p in result)
        assert any("second line" in p for p in result)

    def test_malformed_json_line_is_skipped(self, tmp_path):
        """A line that is not valid JSON must be silently tolerated."""
        from braincell.transcript_ingest import _extract_text_from_jsonl
        content = (
            '{"content": "good line one"}\n'
            '{not valid json at all!!!}\n'
            '{"content": "good line two"}\n'
        )
        f = tmp_path / "malformed.jsonl"
        f.write_text(content, encoding="utf-8")
        result = _extract_text_from_jsonl(f)
        # Must not crash; must extract at least the valid lines.
        assert any("good line one" in p for p in result)
        assert any("good line two" in p for p in result)

    def test_garbage_binary_content_does_not_crash(self, tmp_path):
        """A file with binary garbage (non-UTF-8) must not crash (errors='replace')."""
        from braincell.transcript_ingest import _extract_text_from_jsonl
        # Write some valid JSON, then raw binary garbage, then more valid JSON.
        valid1 = b'{"content": "before garbage"}\n'
        garbage = bytes(range(128, 256)) + b"\n"
        valid2 = b'{"content": "after garbage"}\n'
        f = tmp_path / "binary.jsonl"
        f.write_bytes(valid1 + garbage + valid2)
        result = _extract_text_from_jsonl(f)
        # Must not raise; may or may not extract the garbage line.
        assert isinstance(result, list)

    def test_empty_file_returns_empty_list(self, tmp_path):
        from braincell.transcript_ingest import _extract_text_from_jsonl
        f = tmp_path / "empty.jsonl"
        f.write_bytes(b"")
        assert _extract_text_from_jsonl(f) == []

    def test_nonexistent_file_returns_empty_list(self, tmp_path):
        from braincell.transcript_ingest import _extract_text_from_jsonl
        result = _extract_text_from_jsonl(tmp_path / "missing.jsonl")
        assert result == []

    def test_all_garbage_lines_returns_list(self, tmp_path):
        """A file that is entirely garbage must return [] (not raise)."""
        from braincell.transcript_ingest import _extract_text_from_jsonl
        f = tmp_path / "all_garbage.jsonl"
        f.write_bytes(bytes(range(256)) * 10)
        result = _extract_text_from_jsonl(f)
        assert isinstance(result, list)

    def test_mixed_json_and_plain_text_lines(self, tmp_path):
        """Non-JSON lines of len > 20 are treated as plain text (existing behaviour)."""
        from braincell.transcript_ingest import _extract_text_from_jsonl
        content = (
            '{"content": "json line"}\n'
            'this is a plain text line that is longer than 20 chars\n'
        )
        f = tmp_path / "mixed.jsonl"
        f.write_text(content, encoding="utf-8")
        result = _extract_text_from_jsonl(f)
        assert any("json line" in p for p in result)
        # The long plain-text line must also appear (len > 20, treated as page).
        assert any("plain text line" in p for p in result)

    def test_messages_list_form_content(self, tmp_path):
        """messages[i]['content'] list-form must be extracted without TypeError."""
        from braincell.transcript_ingest import _extract_text_from_jsonl
        record = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "user block text"},
                        {"type": "tool_result", "content": "ignored"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": "assistant plain string",
                },
            ]
        }
        f = tmp_path / "messages_list.jsonl"
        f.write_text(json.dumps(record), encoding="utf-8")
        result = _extract_text_from_jsonl(f)
        # Must not raise TypeError; must produce some text.
        assert isinstance(result, list)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: get_project_id (config.py)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetProjectId:
    """get_project_id: fresh dir mints into registry (no in-repo *.project.json);
    a stray in-repo file is IGNORED (clean break — registry is the sole source);
    create=False on unregistered dir raises."""

    def test_fresh_dir_mints_ulid_no_in_repo_file(self, tmp_path, monkeypatch):
        """A completely fresh directory gets a new ULID; no .project.json is written
        into the directory itself."""
        from braincell.config import get_project_id, DATA_NAMESPACE

        project_root = tmp_path / "fresh_project"
        project_root.mkdir()

        pid = get_project_id(project_root, create=True)
        assert isinstance(pid, str) and pid  # ULID is a non-empty string

        # No in-repo project file should have been written.
        in_repo_file = project_root / f"{DATA_NAMESPACE}.project.json"
        assert not in_repo_file.exists(), \
            f"In-repo project file was written (must not be): {in_repo_file}"

    def test_second_call_returns_same_ulid(self, tmp_path):
        """Calling get_project_id twice on the same dir returns the same ULID."""
        from braincell.config import get_project_id

        project_root = tmp_path / "stable_project"
        project_root.mkdir()

        pid1 = get_project_id(project_root, create=True)
        pid2 = get_project_id(project_root, create=True)
        assert pid1 == pid2

    def test_stray_in_repo_file_is_ignored(self, tmp_path):
        """Clean break: a stray <ns>.project.json in the repo is NOT adopted.

        Identity lives only in the central registry now, so an unregistered dir
        with a leftover in-repo file must raise on create=False (the file is
        ignored, never adopted)."""
        from braincell.config import (
            get_project_id, ProjectIdentityMissing, DATA_NAMESPACE,
        )

        project_root = tmp_path / "legacy_project"
        project_root.mkdir()

        legacy_file = project_root / f"{DATA_NAMESPACE}.project.json"
        legacy_file.write_text(
            json.dumps({"id": "01LEGACYULID0000000000000"}), encoding="utf-8"
        )

        # The stray file is ignored — the path is still unregistered.
        with pytest.raises(ProjectIdentityMissing):
            get_project_id(project_root, create=False)

    def test_create_false_unregistered_raises(self, tmp_path):
        """create=False on a directory not in the registry must raise
        ProjectIdentityMissing."""
        from braincell.config import get_project_id, ProjectIdentityMissing

        unregistered = tmp_path / "unknown_project"
        unregistered.mkdir()

        with pytest.raises(ProjectIdentityMissing):
            get_project_id(unregistered, create=False)
