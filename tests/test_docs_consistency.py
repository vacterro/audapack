"""What the repository says about itself has to still be true.

Filed as MARKHUNT T-135: three sources disagreed about the version, one README
carried three different widget test counts and a pytest count half the real
one, and a wiki page was linked from nowhere. Numbers nobody re-measures rot;
these tests are the re-measuring.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_the_three_version_sources_agree():
    """VERSION said 0.2.2 while pyproject and __init__ said 0.2.3."""
    from audapack import __version__

    version_file = read("VERSION").strip()
    pyproject = re.search(r'^version = "([^"]+)"', read("pyproject.toml"), re.MULTILINE)
    assert pyproject, "pyproject.toml has no version"
    assert version_file == pyproject.group(1) == __version__, (
        f"VERSION={version_file} pyproject={pyproject.group(1)} __init__={__version__}"
    )


def test_the_changelog_leads_with_the_current_version():
    from audapack import __version__

    heading = re.search(r"^## \[([^\]]+)\]", read("CHANGELOG.md"), re.MULTILINE)
    assert heading, "CHANGELOG.md has no version heading"
    assert heading.group(1) == __version__


@pytest.mark.parametrize("readme", ["README.md", "README.ru.md"])
def test_no_readme_claims_a_test_count(readme):
    """A count in a badge is a number nobody re-measures.

    README.md carried Tests-374 and Widget-156 against 763 and 257, and its own
    file tree said 152 across 23 suites -- three different answers in one file.
    """
    text = read(readme)
    for pattern in (r"\d+\s*%20PASS", r"\d+\s+PASS", r"\d+ Node", r"\d+ теста", r"\d+ тестов"):
        assert not re.search(pattern, text), f"{readme} still claims a count: {pattern}"


@pytest.mark.parametrize("readme", ["README.md", "README.ru.md"])
def test_the_readme_release_badge_matches_the_version(readme):
    """CORE-004 (audit/1.md): README called VERSION canonical and showed 0.2.2.

    VERSION, pyproject and __init__ were reconciled to 0.2.3 by T-135, but both
    release badges still advertised 0.2.2 -- so the one number a reader actually
    sees was the stale one.
    """
    from audapack import __version__

    badges = set(re.findall(r"badge/(?:release|релиз)-v([0-9][^-]*)-", read(readme)))
    assert badges == {__version__}, f"{readme} advertises {sorted(badges)}, package is {__version__}"


@pytest.mark.parametrize("readme", ["README.md", "README.ru.md"])
def test_every_wiki_page_is_linked_from_the_readme(readme):
    """Audit-Campaign-Engine.md existed and README.md linked five of six."""
    linked = set(re.findall(r"docs/wiki/([A-Za-z0-9._-]+\.md)", read(readme)))
    on_disk = {path.name for path in (ROOT / "docs" / "wiki").glob("*.md")}
    assert on_disk - linked == set(), f"{readme} does not link: {sorted(on_disk - linked)}"


@pytest.mark.parametrize("readme", ["README.md", "README.ru.md"])
def test_no_readme_link_points_at_a_missing_file(readme):
    text = read(readme)
    for target in re.findall(r"\]\((?!https?:)([^)#]+)\)", text):
        assert (ROOT / target.strip()).exists(), f"{readme} links missing {target}"


def test_the_example_config_is_loadable_and_has_no_invalid_key(tmp_path):
    """It advertised no `launchers` section at all -- the one an operator edits."""
    import dataclasses
    import shutil

    from audapack.config import (
        AuditsConfig,
        BridgeConfig,
        PackingConfig,
        UIConfig,
        load_config,
    )

    example = json.loads(read("config.example.json"))
    for name, cls in (
        ("packing", PackingConfig),
        ("audits", AuditsConfig),
        ("bridge", BridgeConfig),
        ("ui", UIConfig),
    ):
        known = {field.name for field in dataclasses.fields(cls)}
        unknown = set(example.get(name, {})) - known
        assert not unknown, f"config.example.json {name} has retired keys: {sorted(unknown)}"

    assert example.get("launchers"), "the example must show the launchers section"
    shutil.copy(ROOT / "config.example.json", tmp_path / "config.json")
    loaded = load_config(tmp_path)
    assert loaded.launchers, "the example's launchers did not survive a load"


# --------------------------------------------------- T-137: the quiet fallback
#
# audapack/ui/ predates the audit pipeline and has NO reference to any of it,
# so a fallback that opened quietly looked like a working AUDAPACK with the
# audits mysteriously missing. The only warning was a line on stderr, and the
# documented ways in -- AUDAPACK.vbs and pythonw -- both discard it.


def test_the_tkinter_fallback_is_announced_where_it_can_be_seen():
    from unittest.mock import patch

    from audapack import app

    with patch.object(app.sys, "platform", "win32"), \
         patch("ctypes.windll", create=True) as windll:
        app._warn_qt_missing("No module named 'PySide6'")
    windll.user32.MessageBoxW.assert_called_once()
    shown = windll.user32.MessageBoxW.call_args.args[1]
    assert "PySide6" in shown
    assert "audit runs" in shown


def test_a_missing_message_box_never_stops_the_app_opening():
    from unittest.mock import patch

    from audapack import app

    with patch.object(app.sys, "platform", "win32"), \
         patch("ctypes.windll", create=True) as windll:
        windll.user32.MessageBoxW.side_effect = OSError("no user32")
        app._warn_qt_missing("boom")  # must not raise


def test_the_fallback_names_what_the_old_window_cannot_do():
    """If audapack/ui/ ever learns one of these, the warning must stop saying it."""
    from audapack.app import TKINTER_FALLBACK_MISSING

    legacy = (ROOT / "audapack" / "ui").rglob("*.py")
    legacy_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in legacy)
    for marker in ("audit_runs", "queue_position", "arrange_worker_windows", "compact_rows"):
        assert marker not in legacy_text, (
            f"audapack/ui/ now references {marker}; TKINTER_FALLBACK_MISSING is stale"
        )
    assert TKINTER_FALLBACK_MISSING
