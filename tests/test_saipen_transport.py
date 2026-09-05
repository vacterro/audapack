from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from audapack.inaudit_capture import InauditCaptureError
from audapack.saipen_transport import SaipenTransportError, enqueue_file
from tests.test_inaudit_assignment import _seed


def _bind(root: Path, home: Path):
    (root / ".saipen").mkdir(exist_ok=True)
    (root / ".saipen" / "STATE.md").write_text(
        f"---\nsaipen_home: {json.dumps(str(home))}\n---\n", encoding="utf-8")


def _fake_cli(tmp_path, monkeypatch):
    home = tmp_path / "SAIPEN home"
    (home / "tools").mkdir(parents=True)
    (home / "tools" / "saipen.py").touch()
    operations = {}
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert args[2:4] == ["audit", "enqueue"]
        assert kwargs["timeout"] == 30
        root = Path(args[args.index("--project-root") + 1])
        body = Path(args[args.index("--file") + 1]).read_bytes()
        op = args[args.index("--operation-id") + 1]
        digest = hashlib.sha256(body).hexdigest()
        duplicate = op in operations
        if duplicate:
            data = dict(operations[op])
            assert data["sha256"] == digest
        else:
            layer = 40 + len(operations)
            rel = f"audit/{layer}.md"
            (root / "audit").mkdir(exist_ok=True)
            (root / rel).write_bytes(body)
            data = {"ok": True, "code": "AUDIT_ENQUEUED", "layer": layer, "rel": rel,
                    "sha256": digest, "producer": "audapack", "producer_operation_id": op}
            operations[op] = data
        data = dict(data, idempotent=duplicate, present=(root / data["rel"]).exists())
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")

    monkeypatch.setattr("audapack.saipen_transport.run_hidden", run)
    return home, calls


def test_managed_capture_uses_cli_allocation_and_bare_cc(tmp_path, monkeypatch):
    store, payload, project = _seed(tmp_path)
    home, calls = _fake_cli(tmp_path, monkeypatch)
    _bind(Path(project.source_path), home)
    result = store.assign(payload["capture_id"], project.id, [project], action="GG")
    assert Path(result["assigned_path"]).name == "40.md"
    assert Path(result["assigned_path"]).read_bytes() == payload["text"].encode()
    assert result["command"] == "saipen cc"
    assert payload["capture_id"] in calls[0]
    assert store.get(payload["capture_id"])["record"]["producer_operation_id"] == payload["capture_id"]


def test_metadata_failure_retries_same_layer_even_after_consumption(tmp_path, monkeypatch):
    store, payload, project = _seed(tmp_path)
    home, calls = _fake_cli(tmp_path, monkeypatch)
    root = Path(project.source_path)
    _bind(root, home)
    original = store._atomic_json

    def fail(path, data):
        if data.get("status") == "ASSIGNED":
            raise OSError("disk full")
        return original(path, data)

    monkeypatch.setattr(store, "_atomic_json", fail)
    with pytest.raises(OSError, match="disk full"):
        store.assign(payload["capture_id"], project.id, [project])
    target = root / "audit" / "40.md"
    assert target.read_bytes() == payload["text"].encode()
    target.unlink()  # SAIPEN may finish before AUDAPACK recovers metadata.
    monkeypatch.setattr(store, "_atomic_json", original)
    result = store.assign(payload["capture_id"], project.id, [project], action="CC")
    assert result["duplicate"] and result["present"] is False
    assert not list((root / "audit").glob("*.md"))
    assert len(calls) == 2 and calls[0] == calls[1]


def test_missing_runtime_keeps_capture_and_never_falls_back(tmp_path):
    store, payload, project = _seed(tmp_path)
    root = Path(project.source_path)
    _bind(root, tmp_path / "missing")
    with pytest.raises(SaipenTransportError, match="missing bound CLI"):
        store.assign(payload["capture_id"], project.id, [project])
    assert not (root / "audit").exists()
    assert store.get(payload["capture_id"])["text"] == payload["text"]


def test_pending_delivery_cannot_switch_projects(tmp_path, monkeypatch):
    from audapack.models import Project
    store, payload, project = _seed(tmp_path)
    _bind(Path(project.source_path), tmp_path / "missing")
    with pytest.raises(SaipenTransportError):
        store.assign(payload["capture_id"], project.id, [project])
    other = Project(id="other", display_name="Other", source_path=str(tmp_path / "Other"))
    with pytest.raises(InauditCaptureError, match="another project"):
        store.assign(payload["capture_id"], other.id, [project, other])


@pytest.mark.parametrize("failure", ["timeout", "refused", "invalid_json", "wrong_hash"])
def test_cli_failure_is_not_a_success(tmp_path, monkeypatch, failure):
    home, _calls = _fake_cli(tmp_path, monkeypatch)
    root = tmp_path / "project"
    root.mkdir()
    _bind(root, home)
    body = tmp_path / "body.md"
    body.write_bytes(b"exact\r\nbytes")
    def run(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 30)
        data = {"ok": False, "code": "WRITER_BUSY"} if failure == "refused" else {"ok": True, "sha256": "wrong"}
        return subprocess.CompletedProcess(args, 0, "oops" if failure == "invalid_json" else json.dumps(data), "")
    monkeypatch.setattr("audapack.saipen_transport.run_hidden", run)
    with pytest.raises(SaipenTransportError):
        enqueue_file(root, body, str(uuid.uuid4()))


def test_real_saipen_concurrent_producers_retry_and_monotonic_ids(tmp_path):
    home = os.environ.get("SAIPEN_TEST_HOME")
    if not home:
        pytest.skip("Set SAIPEN_TEST_HOME to test the installed producer CLI")
    root = tmp_path / "project"
    root.mkdir()
    _bind(root, Path(home))
    bodies = [tmp_path / "A.md", tmp_path / "B.md"]
    for i, body in enumerate(bodies):
        body.write_bytes(f"# Capture {i}\r\nExact bytes\r\n".encode())
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda pair: enqueue_file(root, *pair), zip(bodies, ids, strict=True)))
    assert sorted(result["layer"] for result in results) == [1, 2]
    for result, body in zip(results, bodies, strict=True):
        assert (root / result["rel"]).read_bytes() == body.read_bytes()
    retry = enqueue_file(root, bodies[0], ids[0])
    assert retry["idempotent"] and retry["layer"] == results[0]["layer"]
    (root / "audit" / "2.md").unlink()
    new = enqueue_file(root, bodies[1], str(uuid.uuid4()))
    assert new["layer"] == 3


def test_managed_mirror_retries_after_layer_disappears(tmp_path, monkeypatch):
    from audapack.bridge.storage import mirror_project_audits
    from tests.test_audit_mirror import _audit_dir, _config, _handoff
    home, calls = _fake_cli(tmp_path, monkeypatch)
    root = tmp_path / "project"
    root.mkdir()
    _bind(root, home)
    src = _audit_dir(tmp_path)
    config = _config(tmp_path, mirror_into_project=True)
    first = mirror_project_audits(config, root, src, _handoff(src))
    assert first == [root / "audit" / "40.md"]
    first[0].unlink()
    assert mirror_project_audits(config, root, src, _handoff(src)) == []
    assert len(calls) == 2
    assert not list((root / "audit").glob("*.md"))
