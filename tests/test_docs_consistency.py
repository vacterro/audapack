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
