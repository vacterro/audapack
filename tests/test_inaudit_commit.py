"""Red-first regressions for CORE-001 durable existing-layer commit.

Every durable boundary is exercised: normal commit, crash recovery, and the
two mandatory invariants -- exact old/new bytes (never partial) and no
resurrection of a consumed layer.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

import audapack.inaudit_commit as ic


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _journal(root, target_name, old: bytes, new: bytes) -> None:
    (root / "audit" / f".save-{target_name}.save-journal").write_text(
        '{"schema_version":1,"target":"%s","old_sha256":"%s","new_sha256":"%s"}'
        % (target_name, _sha(old), _sha(new))
    )


def _project_dir():
    root = Path(tempfile.mkdtemp())
    (root / "audit").mkdir()
    return root


def _layer(root, name, text):
    p = root / "audit" / name
    p.write_bytes(text.encode("utf-8"))
    return p


@pytest.fixture(autouse=True)
def _clear_hooks():
    ic.commit_stage_hooks.clear()
    yield
    ic.commit_stage_hooks.clear()


def _fire_at(stage, fn):
    def _hook(name):
        if name == stage:
            fn()

    ic.commit_stage_hooks.append(_hook)


class TestNormalCommit:
    def test_commits_exact_new_bytes(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.COMMITTED
        assert layer.read_bytes() == b"NEW"
        assert res.committed_bytes == b"NEW"

    def test_replay_is_idempotent(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "NEW")
        res = ic.commit_existing_layer(layer, b"NEW", b"NEW")
        assert res.outcome == ic.COMMITTED
        assert layer.read_bytes() == b"NEW"

    def test_no_journal_left_behind(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")
        ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert not [p for p in root.iterdir() if p.name.startswith(".save-")]


class TestRefusal:
    def test_missing_target_never_recreated(self, tmp_path):
        root = _project_dir()
        layer = root / "audit" / "1.md"  # not created
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.MISSING_OR_CONSUMED
        assert not layer.exists()

    def test_foreign_bytes_survive(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "FOREIGN")
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.CHANGED_EXTERNALLY
        assert layer.read_bytes() == b"FOREIGN"

    def test_stale_expected_refused(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "AAA")
        res = ic.commit_existing_layer(layer, b"ZZZ", b"NEW")
        assert res.outcome == ic.CHANGED_EXTERNALLY


class TestCrashRecoveryDeterministic:
    def test_recover_new(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "NEW")
        _journal(root, "1.md", b"OLD", b"NEW")
        res = ic.recover_pending_layer_commit(layer)
        assert res.outcome == ic.RECOVERED_NEW
        assert not (root / "audit" / ".save-1.md.save-journal").exists()

    def test_recover_old(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")
        _journal(root, "1.md", b"OLD", b"NEW")
        res = ic.recover_pending_layer_commit(layer)
        assert res.outcome == ic.RECOVERED_OLD
        assert layer.read_bytes() == b"OLD"

    def test_recover_absent_leaves_gone(self, tmp_path):
        root = _project_dir()
        layer = root / "audit" / "1.md"  # consumed
        _journal(root, "1.md", b"OLD", b"NEW")
        res = ic.recover_pending_layer_commit(layer)
        assert res.outcome == ic.ABSENT
        assert not layer.exists()

    def test_recover_conflict_leaves_foreign(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "FOREIGN")
        _journal(root, "1.md", b"OLD", b"NEW")
        res = ic.recover_pending_layer_commit(layer)
        assert res.outcome == ic.CONFLICT
        assert layer.read_bytes() == b"FOREIGN"

    def test_dir_scan_clears_all_journals(self, tmp_path):
        root = _project_dir()
        _layer(root, "3.md", "NEW")
        _journal(root, "3.md", b"OLD", b"NEW")
        _layer(root, "1.md", "widget")  # canonical, must be untouched
        results = ic.recover_inaudit_dir_commits(root / "audit")
        assert any(r.outcome == ic.RECOVERED_NEW for r in results)
        assert (root / "audit" / "1.md").read_bytes() == b"widget"


class TestFailureInjection:
    def test_fails_before_commit_keeps_old(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")

        def _boom():
            raise RuntimeError("injected")

        _fire_at(ic.STAGE_BEFORE_COMMIT, _boom)
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.IO_FAILED
        assert layer.read_bytes() == b"OLD"

    def test_partial_temp_write_keeps_old(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")

        def _boom():
            raise RuntimeError("injected")

        _fire_at(ic.STAGE_TEMP_WRITE, _boom)
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.IO_FAILED
        assert layer.read_bytes() == b"OLD"

    def test_journal_fsync_failure_keeps_old(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")

        def _boom():
            raise RuntimeError("injected")

        _fire_at(ic.STAGE_JOURNAL_FSYNC, _boom)
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.IO_FAILED
        assert layer.read_bytes() == b"OLD"

    def test_external_change_before_commit_refused(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")

        def _mutate():
            layer.write_bytes(b"FOREIGN")

        _fire_at(ic.STAGE_BEFORE_COMMIT, _mutate)
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.CHANGED_EXTERNALLY
        assert layer.read_bytes() == b"FOREIGN"

    def test_consumption_before_commit_not_resurrected(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")

        def _consume():
            layer.unlink()

        _fire_at(ic.STAGE_BEFORE_COMMIT, _consume)
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.MISSING_OR_CONSUMED
        assert not layer.exists()

    def test_cleanup_failure_still_committed(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")

        def _boom():
            raise RuntimeError("injected")

        _fire_at(ic.STAGE_CLEANUP, _boom)
        ic.commit_existing_layer(layer, b"OLD", b"NEW")
        # Commit itself succeeded; a cleanup error leaves durable bytes.
        assert layer.read_bytes() == b"NEW"


class TestSubprocessKill:
    """Crash mid-commit; parent recovers to exact old or new."""

    def _kill_child(self, root, stage):
        code = textwrap.dedent(
            """
            import os, sys
            from pathlib import Path
            sys.path.insert(0, {cwd!r})
            import audapack.inaudit_commit as ic
            root = Path({root!r})
            layer = root / "audit" / "1.md"
            layer.write_bytes(b"OLD")
            def _killer(name):
                if name == {stage!r}:
                    os._exit(7)
            ic.commit_stage_hooks.append(_killer)
            ic.commit_existing_layer(layer, b"OLD", b"NEW")
            os._exit(0)
            """
        ).format(cwd=str(Path.cwd()), root=str(root), stage=stage)
        return subprocess.run([sys.executable, "-c", code], capture_output=True)

    def test_kill_before_commit_recovers_to_old(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")
        proc = self._kill_child(root, ic.STAGE_BEFORE_COMMIT)
        assert proc.returncode == 7
        res = ic.recover_pending_layer_commit(layer)
        assert res.outcome in (ic.RECOVERED_OLD, ic.ABSENT)
        assert layer.read_bytes() == b"OLD" or not layer.exists()

    def test_kill_at_cleanup_recovers_to_new(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")
        proc = self._kill_child(root, ic.STAGE_CLEANUP)
        assert proc.returncode == 7
        res = ic.recover_pending_layer_commit(layer)
        assert res.outcome in (ic.RECOVERED_NEW, ic.RECOVERED_OLD)
        # Never a truncated hybrid.
        assert layer.read_bytes() in (b"NEW", b"OLD")


# --------------------------------------------------------------------------
# CORE-001 POSIX: real atomic EXCHANGE, never a generic os.replace fallback.
# The defect was that Python's posix module does not expose renameat2, so the
# code fell through to os.replace + a bogus inode re-check that deleted a
# successfully replaced canonical layer. These tests exercise the POSIX path
# on any host by swapping in an emulated exchange (the real one runs under the
# Linux-only class below).
# --------------------------------------------------------------------------

def _emulated_exchange(replacement, target):
    """Swap two existing entries, mirroring renameat2(RENAME_EXCHANGE)."""
    replacement = Path(replacement)
    target = Path(target)
    if not target.exists():
        raise ic._DestinationMissing()
    hold = target.with_name(target.name + ".exchange-hold")
    os.replace(target, hold)
    os.replace(replacement, target)
    os.replace(hold, replacement)


@pytest.fixture
def posix_commit(monkeypatch):
    monkeypatch.setattr(ic, "_PLATFORM", "linux")
    monkeypatch.setattr(ic, "_native_rename_exchange", _emulated_exchange)


class TestPosixExistingOnlyCommit:
    def test_normal_commit_keeps_canonical_present(self, tmp_path, posix_commit):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.COMMITTED
        assert layer.read_bytes() == b"NEW"
        assert layer.is_file()

    def test_expected_inode_change_is_not_consume(self, tmp_path, posix_commit):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")
        before = layer.stat().st_ino
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.COMMITTED, res
        assert layer.read_bytes() == b"NEW"
        # An exchange legitimately changes the inode; that must never be read
        # as a concurrent consume.
        assert layer.stat().st_ino != before or before == 0

    def test_missing_destination_at_exchange_is_consumed(self, tmp_path, monkeypatch, posix_commit):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")

        def _exchange_then_gone(replacement, target):
            Path(target).unlink()
            raise ic._DestinationMissing()

        monkeypatch.setattr(ic, "_native_rename_exchange", _exchange_then_gone)
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.MISSING_OR_CONSUMED
        assert not layer.exists()

    def test_exchange_leaves_old_recovery_evidence(self, tmp_path, posix_commit):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")
        backup = ic._backup_path(layer)
        seen = {}

        def _capture(name):
            if name == ic.STAGE_AFTER_COMMIT:
                seen["canonical"] = layer.read_bytes()
                seen["backup"] = backup.read_bytes() if backup.exists() else None

        ic.commit_stage_hooks.append(_capture)
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.COMMITTED
        assert seen["canonical"] == b"NEW"
        assert seen["backup"] == b"OLD"

    def test_cleanup_failure_leaves_new_bytes(self, tmp_path, posix_commit):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")

        def _boom():
            raise RuntimeError("injected")

        _fire_at(ic.STAGE_CLEANUP, _boom)
        ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert layer.read_bytes() == b"NEW"

    def test_unsupported_platform_refuses_before_mutation(self, tmp_path, monkeypatch, posix_commit):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")

        def _no_primitive(replacement, target):
            raise ic._UnsupportedSafeReplace()

        monkeypatch.setattr(ic, "_native_rename_exchange", _no_primitive)
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.IO_FAILED
        assert "unsupported" in res.detail
        assert layer.read_bytes() == b"OLD"
        assert not [p for p in root.iterdir() if p.name.startswith(".save-")]

    def test_crash_after_exchange_recovers_to_old_or_new(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")
        code = textwrap.dedent(
            """
            import os, sys
            from pathlib import Path
            sys.path.insert(0, {cwd!r})
            import audapack.inaudit_commit as ic
            ic._PLATFORM = "linux"
            def _exchange(replacement, target):
                replacement = Path(replacement); target = Path(target)
                if not target.exists():
                    raise ic._DestinationMissing()
                hold = target.with_name(target.name + ".exchange-hold")
                os.replace(target, hold)
                os.replace(replacement, target)
                os.replace(hold, replacement)
            ic._native_rename_exchange = _exchange
            root = Path({root!r})
            layer = root / "audit" / "1.md"
            layer.write_bytes(b"OLD")
            def _killer(name):
                if name == ic.STAGE_AFTER_COMMIT:
                    os._exit(7)
            ic.commit_stage_hooks.append(_killer)
            ic.commit_existing_layer(layer, b"OLD", b"NEW")
            os._exit(0)
            """
        ).format(cwd=str(Path.cwd()), root=str(root))
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
        assert proc.returncode == 7
        res = ic.recover_pending_layer_commit(layer)
        assert res.outcome in (ic.RECOVERED_NEW, ic.RECOVERED_OLD, ic.ABSENT)
        assert not layer.exists() or layer.read_bytes() in (b"NEW", b"OLD")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native renameat2")
class TestLinuxNativeExchange:
    def test_native_exchange_is_available_and_swaps(self, tmp_path):
        impl = ic._load_rename_exchange()
        assert impl is not None, "libc.renameat2 must be usable on Linux"
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.write_bytes(b"A")
        b.write_bytes(b"B")
        impl(a, b)
        assert a.read_bytes() == b"B"
        assert b.read_bytes() == b"A"

    def test_native_exchange_missing_target_raises_consumed(self, tmp_path):
        impl = ic._load_rename_exchange()
        a = tmp_path / "a"
        a.write_bytes(b"A")
        target = tmp_path / "gone"
        with pytest.raises(ic._DestinationMissing):
            impl(a, target)

    def test_real_linux_commit(self, tmp_path):
        root = _project_dir()
        layer = _layer(root, "1.md", "OLD")
        res = ic.commit_existing_layer(layer, b"OLD", b"NEW")
        assert res.outcome == ic.COMMITTED
        assert layer.read_bytes() == b"NEW"
