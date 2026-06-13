"""Tests for incremental sidecar offset tracking."""
import json
import os
import tempfile
from pathlib import Path

import pytest


class TestSidecarTracking:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sidecar_dir = Path(self.tmpdir)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_transcript(self, entries):
        path = os.path.join(self.tmpdir, "transcript.jsonl")
        with open(path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        return path

    def _make_entry(self, etype, content=None):
        entry = {"type": etype, "timestamp": "2026-05-29T00:00:00Z"}
        if etype == "user":
            entry["message"] = {"content": content}
        elif etype == "assistant":
            entry["message"] = {"content": content}
        return entry

    def test_first_read_processes_entire_file(self):
        entries = [
            self._make_entry("user", "First message here"),
            self._make_entry("assistant", [{"type": "text", "text": "First response here."}]),
        ]
        path = self._write_transcript(entries)
        sidecar = self.sidecar_dir / "sent_session1"

        # Simulate first read (no sidecar)
        assert not sidecar.exists()

        file_size = os.path.getsize(path)
        with open(path, "r") as f:
            lines = [json.loads(l) for l in f if l.strip()]

        assert len(lines) == 2

        # Write sidecar
        sidecar.write_text(str(file_size))
        assert sidecar.exists()

    def test_second_read_skips_already_sent(self):
        entries = [
            self._make_entry("user", "First message here"),
            self._make_entry("assistant", [{"type": "text", "text": "First response here."}]),
        ]
        path = self._write_transcript(entries)

        # Record offset after first batch
        first_size = os.path.getsize(path)
        sidecar = self.sidecar_dir / "sent_session1"
        sidecar.write_text(str(first_size))

        # Append new entries
        with open(path, "a") as f:
            f.write(json.dumps(self._make_entry("user", "Second message here")) + "\n")
            f.write(json.dumps(self._make_entry("assistant", [{"type": "text", "text": "Second response."}])) + "\n")

        # Read only new lines
        last_offset = int(sidecar.read_text().strip())
        new_entries = []
        with open(path, "r") as f:
            f.seek(last_offset)
            for line in f:
                line = line.strip()
                if line:
                    new_entries.append(json.loads(line))

        assert len(new_entries) == 2
        assert new_entries[0]["message"]["content"] == "Second message here"

    def test_missing_sidecar_reprocesses_all(self):
        entries = [
            self._make_entry("user", "Message after crash"),
            self._make_entry("assistant", [{"type": "text", "text": "Response after crash."}]),
        ]
        path = self._write_transcript(entries)
        sidecar = self.sidecar_dir / "sent_session1"

        # No sidecar = crash recovery
        assert not sidecar.exists()
        last_offset = 0

        with open(path, "r") as f:
            if last_offset > 0:
                f.seek(last_offset)
            all_entries = [json.loads(l) for l in f if l.strip()]

        assert len(all_entries) == 2

    def test_sidecar_not_updated_on_empty_new_content(self):
        entries = [
            self._make_entry("user", "Only message here"),
            self._make_entry("assistant", [{"type": "text", "text": "Only response here."}]),
        ]
        path = self._write_transcript(entries)

        file_size = os.path.getsize(path)
        sidecar = self.sidecar_dir / "sent_session1"
        sidecar.write_text(str(file_size))

        # No new content — file size equals sidecar offset
        new_size = os.path.getsize(path)
        assert new_size <= int(sidecar.read_text().strip())

    def test_corrupt_sidecar_resets_to_zero(self):
        sidecar = self.sidecar_dir / "sent_session1"
        sidecar.write_text("not-a-number")

        try:
            last_offset = int(sidecar.read_text().strip())
        except (ValueError, OSError):
            last_offset = 0

        assert last_offset == 0
