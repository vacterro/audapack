from __future__ import annotations

from pathlib import Path

from audapack.inaudit_capture import InauditCaptureStore
from audapack.models import Project


def _projects(tmp_path: Path) -> tuple[Project, Project]:
    audapack = tmp_path / "AUDAPACK"
    fast = tmp_path / "FastPrompter"
    (audapack / "audapack" / "bridge").mkdir(parents=True)
    (audapack / "audapack" / "bridge" / "browser_dispatch.py").write_text("class BrowserDispatcher: pass")
    fast.mkdir()
    (fast / "__FastPrompter__.pyw").write_text("pass")
    return (
        Project(id="audapack", display_name="AUDAPACK", source_path=str(audapack)),
        Project(id="fastprompter", display_name="FastPrompter", source_path=str(fast)),
    )


def test_exact_project_path_is_very_high_confidence(tmp_path: Path):
    store = InauditCaptureStore(tmp_path / "runtime")
    audapack, fast = _projects(tmp_path)
    result = store.classify(f"Repair {audapack.source_path}\\audapack\\bridge\\browser_dispatch.py", [audapack, fast])
    assert result["project_id"] == "audapack"
    assert result["confidence"] == 1.0
    assert any("exact path" in value for value in result["evidence"])


def test_exact_name_is_strong_and_unique_file_is_suggested(tmp_path: Path):
    store = InauditCaptureStore(tmp_path / "runtime")
    audapack, fast = _projects(tmp_path)
    exact = store.classify("AUDAPACK dispatcher repair", [audapack, fast])
    unique = store.classify("Please fix __FastPrompter__.pyw", [audapack, fast])
    assert exact["project_id"] == "audapack" and exact["state"] == "STRONG"
    assert unique["project_id"] == "fastprompter" and unique["state"] == "SUGGESTED"
    assert any("unique file" in value for value in unique["evidence"])


def test_unique_project_symbol_is_explainable(tmp_path: Path):
    store = InauditCaptureStore(tmp_path / "runtime")
    audapack, fast = _projects(tmp_path)
    result = store.classify("BrowserDispatcher recovery contract", [audapack, fast])
    assert result["project_id"] == "audapack"
    assert result["state"] == "SUGGESTED"
    assert any('unique symbol "BrowserDispatcher"' == value for value in result["evidence"])


def test_user_alias_participates_in_cached_identity_index(tmp_path: Path):
    store = InauditCaptureStore(tmp_path / "runtime")
    audapack, fast = _projects(tmp_path)
    audapack.inaudit_aliases = ["AICHATBUTTONS"]
    first = store.classify("AICHATBUTTONS capture bus", [audapack, fast])
    index_mtime = store.index_path.stat().st_mtime_ns
    second = store.classify("AICHATBUTTONS capture bus", [audapack, fast])
    assert first["project_id"] == second["project_id"] == "audapack"
    assert store.index_path.stat().st_mtime_ns == index_mtime


def test_ambiguous_text_remains_unassigned(tmp_path: Path):
    store = InauditCaptureStore(tmp_path / "runtime")
    projects = _projects(tmp_path)
    result = store.classify("generic agent architecture", projects)
    assert result["project_id"] == ""
    assert result["state"] == "UNASSIGNED"


def test_affinity_is_low_weight_and_strong_text_wins(tmp_path: Path):
    store = InauditCaptureStore(tmp_path / "runtime")
    audapack, fast = _projects(tmp_path)
    store._save_affinity(
        {"chat": {"last_confirmed_project_id": "audapack", "confirmed_count": 10, "updated_at": "now"}}
    )
    affinity_only = store.classify("generic notes", [audapack, fast], conversation_fingerprint="chat")
    explicit_other = store.classify(
        f"Implement {fast.source_path}\\__FastPrompter__.pyw",
        [audapack, fast],
        conversation_fingerprint="chat",
    )
    assert affinity_only["state"] == "UNASSIGNED"
    assert explicit_other["project_id"] == "fastprompter"
    assert explicit_other["confidence"] == 1.0
