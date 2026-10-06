"""Qt test fixtures shared by the ``tests/ui_qt`` suites."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _placeholder_paths_skip_launch_admission():
    """Placeholder project paths (``V:\\code\\a``) skip OpenCode launch admission.

    Many launcher and double-click tests name a project path that does not
    exist, because they exercise focus reuse and row actions, not admission.
    Real admission refuses a missing source directory, so those tests never
    reached the behaviour they were written for. Admission keeps its real
    behaviour for any path that exists (its own tests use ``tmp_path``).
    """
    import audapack.ui_qt.main_window as main_window
    from audapack.opencode_launch import OpenCodeAdmission

    real = main_window.admit_with_fallback

    def admit(source_path, *args, **kwargs):
        if not Path(source_path).is_dir():
            # What real admission answers for a plain, unmanaged directory.
            return OpenCodeAdmission(managed=False, cwd=Path(source_path), command=None, binding=None)
        return real(source_path, *args, **kwargs)

    with patch.object(main_window, "admit_with_fallback", admit):
        yield
