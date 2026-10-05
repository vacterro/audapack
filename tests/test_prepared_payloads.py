from pathlib import Path

import pytest

from audapack.models import Project
from audapack.prepared_payloads import pin_handoff, read_pinned, resolve_latest_handoff

PROJECTS = (
    Project(id="p-a", display_name="AUDAPACK", source_path="C:/audit"),
    Project(id="p-b", display_name="SAIPEN", source_path="C:/saipen"),
)


def _handoff(folder: Path, name: str, project: str, body: str) -> Path:
    path = folder / name
    path.write_text(f"{project} — SAIHANDOFF — prepared\n{body}\n", encoding="utf-8")
    return path


def test_latest_matches_content_project_and_updates_after_new_capture(tmp_path):
    audapack = _handoff(tmp_path, "a.md", "AUDAPACK", "first")
    wrong = _handoff(tmp_path, "z.md", "SAIPEN", "other")
    import os
    os.utime(audapack, (10, 10))
    os.utime(wrong, (30, 30))
    assert resolve_latest_handoff("p-a", PROJECTS, tmp_path).path == audapack
    new = _handoff(tmp_path, "b.md", "AUDAPACK", "new")
    os.utime(new, (40, 40))
    assert resolve_latest_handoff("p-a", PROJECTS, tmp_path).path == new


def test_pinned_copy_survives_temp_original_and_detects_tampering(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    original = _handoff(drop, "original.md", "AUDAPACK", "exact text")
    pinned = pin_handoff(original, "p-a", PROJECTS, tmp_path / "durable")
    original.unlink()
    assert b"exact text" in read_pinned(pinned.path, pinned.sha256)
    pinned.path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        read_pinned(pinned.path, pinned.sha256)


def test_ambiguous_project_alias_rejected(tmp_path):
    source = _handoff(tmp_path, "source.md", "AUDAPACK", "body")
    projects = PROJECTS + (Project(id="p-c", display_name="AUDAPACK", source_path="C:/other"),)
    assert resolve_latest_handoff("p-a", projects, tmp_path) is None
    with pytest.raises(ValueError, match="ambiguous"):
        pin_handoff(source, "p-a", projects, tmp_path / "durable")
