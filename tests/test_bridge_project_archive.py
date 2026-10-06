"""T-180C/H: Bridge canonical project-archive ensure + download endpoints.

The widget may only ask the Bridge for an archive of an ALREADY registered
project. These tests pin the loopback auth boundary, the read-only project
resolution, the canonical (server-resolved) download, the ensure/reuse/pack
transaction and the size/digest metadata the widget validates before attaching.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from audapack import hot_freshness
from audapack.bridge.server import AudapackBridgeHandler
from audapack.config import AppConfig, save_config
from audapack.models import Project


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _BridgeArchiveFixture(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.audit_root = self.temp_dir / "AUDITING_IMPLEMENTATION"
        self.audit_root.mkdir(parents=True)
        self.archive_dir = self.temp_dir / "archives"
        self.archive_dir.mkdir(parents=True)
        self.source = self.temp_dir / "ProjectA"
        self.source.mkdir()
        (self.source / "hello.txt").write_text("hello audapack", encoding="utf-8")

        self.config = AppConfig()
        self.config.audits.root = str(self.audit_root)
        self.config.bridge.host = "127.0.0.1"
        self.config.bridge.port = 18943
        self.config.bridge.token = "archive_test_token_1234567890"
        self.config.packing.output_dir = str(self.archive_dir)
        self.config.packing.include_timestamp = False
        self.config.projects = [
            Project(
                id="proja",
                display_name="Project A",
                source_path=str(self.source),
                priority_group="MAIN0",
                slot=1,
            ),
            Project(
                id="disabledproj",
                display_name="Disabled",
                source_path=str(self.source),
                priority_group="MAIN0",
                slot=2,
                enabled=False,
            ),
            Project(
                id="nosource",
                display_name="No Source",
                source_path="",
                priority_group="MAIN0",
                slot=3,
            ),
        ]

        class TestHandler(AudapackBridgeHandler):
            pass

        TestHandler.config = self.config
        TestHandler.test_base_dir = str(self.temp_dir)
        save_config(self.config, base_dir=str(self.temp_dir))
        self.server = ThreadingHTTPServer((self.config.bridge.host, self.config.bridge.port), TestHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ helpers

    def _url(self, path: str) -> str:
        return f"http://{self.config.bridge.host}:{self.config.bridge.port}{path}"

    def _token(self) -> str:
        return self.config.bridge.token

    def _ensure_request(self, project_id: str, token: str | None = None):
        return urllib.request.Request(
            self._url(f"/v1/projects/{project_id}/archive/ensure"),
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-ACB-Token": token or self._token()},
        )

    def _get_request(self, project_id: str, token: str | None = None):
        return urllib.request.Request(
            self._url(f"/v1/projects/{project_id}/archive"),
            method="GET",
            headers={"X-ACB-Token": token or self._token()},
        )

    def _post_ensure(self, project_id: str, token: str | None = None):
        return urllib.request.urlopen(self._ensure_request(project_id, token))

    def _get_archive(self, project_id: str, token: str | None = None):
        return urllib.request.urlopen(self._get_request(project_id, token))

    def _http_error(self, request) -> int:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        return ctx.exception.code

    def _error_code(self, request) -> str:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        return json.loads(ctx.exception.read().decode("utf-8"))["error"]["code"]


class TestBridgeProjectArchive(_BridgeArchiveFixture):
    # ------------------------------------------------------------------- auth

    def test_ensure_requires_auth(self):
        req = urllib.request.Request(
            self._url("/v1/projects/proja/archive/ensure"),
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(self._http_error(req), 403)

    def test_download_requires_auth(self):
        req = urllib.request.Request(self._url("/v1/projects/proja/archive"), method="GET")
        self.assertEqual(self._http_error(req), 403)

    # -------------------------------------------------------------- resolution

    def test_unknown_project_is_rejected(self):
        self.assertEqual(self._error_code(self._ensure_request("nope")), "unknown_project")

    def test_disabled_project_is_rejected(self):
        self.assertEqual(self._error_code(self._ensure_request("disabledproj")), "project_disabled")

    def test_project_without_source_is_rejected(self):
        self.assertEqual(self._error_code(self._ensure_request("nosource")), "project_source_missing")

    def test_download_unknown_project_is_rejected(self):
        self.assertEqual(self._error_code(self._get_request("nope")), "unknown_project")

    # ------------------------------------------------------------------ ensure

    def test_first_ensure_packs_then_second_reuses(self):
        with self._post_ensure("proja") as resp:
            self.assertEqual(resp.status, 200)
            first = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(first["ok"])
        self.assertFalse(first["reused"])
        self.assertTrue(first["packed"])
        self.assertEqual(first["display_name"], "Project A")
        archive = self.archive_dir / first["filename"]
        self.assertTrue(archive.is_file())
        self.assertEqual(first["size"], archive.stat().st_size)
        self.assertEqual(first["sha256"], _sha256(archive))

        with self._post_ensure("proja") as resp:
            second = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(second["reused"])
        self.assertFalse(second["packed"])
        self.assertEqual(second["sha256"], first["sha256"])

    def test_stale_source_is_packed_exactly_once(self):
        with self._post_ensure("proja") as resp:
            first = json.loads(resp.read().decode("utf-8"))
        archive = self.archive_dir / first["filename"]
        # Make the source unambiguously newer than the archive.
        newer = archive.stat().st_mtime + 10
        os.utime(self.source / "hello.txt", (newer, newer))

        with self._post_ensure("proja") as resp:
            repacked = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(repacked["packed"])
        self.assertFalse(repacked["reused"])

        # Now make the repacked archive authoritative so the next ensure reuses.
        fresh_archive = self.archive_dir / repacked["filename"]
        older = fresh_archive.stat().st_mtime - 10
        os.utime(self.source / "hello.txt", (older, older))

        with self._post_ensure("proja") as resp:
            reused = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(reused["reused"])
        self.assertFalse(reused["packed"])

    def test_ensure_uses_live_configured_output_directory(self):
        with self._post_ensure("proja") as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual((self.archive_dir / data["filename"]).parent, self.archive_dir)

    # ---------------------------------------------------------------- download

    def test_download_streams_exact_canonical_zip(self):
        with self._post_ensure("proja") as resp:
            data = json.loads(resp.read().decode("utf-8"))
        archive = self.archive_dir / data["filename"]
        with self._get_archive("proja") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "application/zip")
            self.assertEqual(resp.headers.get("Content-Length"), str(archive.stat().st_size))
            self.assertIn("attachment;", resp.headers.get("Content-Disposition", ""))
            self.assertEqual(resp.headers.get("X-AUDAPACK-Archive-SHA256"), _sha256(archive))
            body = resp.read()
        self.assertEqual(body, archive.read_bytes())

    def test_download_before_ensure_reports_missing(self):
        self.assertEqual(self._error_code(self._get_request("proja")), "archive_missing")

    def test_download_ignores_arbitrary_path_query(self):
        with self._post_ensure("proja") as resp:
            data = json.loads(resp.read().decode("utf-8"))
        req = urllib.request.Request(
            self._url("/v1/projects/proja/archive?path=C:/Windows/win.ini"),
            method="GET",
            headers={"X-ACB-Token": self._token()},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.read(), (self.archive_dir / data["filename"]).read_bytes())

    def test_download_rejects_disabled_project_even_when_archive_exists(self):
        with self._post_ensure("proja") as resp:
            data = json.loads(resp.read().decode("utf-8"))
        archive = self.archive_dir / data["filename"]
        self.assertTrue(archive.is_file())

        self.config.projects[0].enabled = False
        self.assertEqual(self._error_code(self._get_request("proja")), "project_disabled")
        self.assertTrue(archive.is_file(), "refusing to serve must not delete the archive")

        self.config.projects[0].enabled = True
        with self._get_archive("proja") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), archive.read_bytes())

    def test_download_rejects_project_without_source(self):
        self.assertEqual(self._error_code(self._get_request("nosource")), "project_source_missing")

    def test_download_rejects_missing_source_directory(self):
        with self._post_ensure("proja") as resp:
            data = json.loads(resp.read().decode("utf-8"))
        archive = self.archive_dir / data["filename"]

        shutil.rmtree(self.source)
        self.assertEqual(self._error_code(self._get_request("proja")), "project_source_unavailable")
        self.assertTrue(archive.is_file(), "refusing to serve must not delete the archive")

        self.source.mkdir()
        with self._get_archive("proja") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), archive.read_bytes())

    def test_path_trick_cannot_reach_another_project(self):
        req = urllib.request.Request(
            self._url("/v1/projects/proja%2f..%2fdisabledproj/archive"),
            method="GET",
            headers={"X-ACB-Token": self._token()},
        )
        self.assertEqual(self._error_code(req), "unknown_project")

    def test_trailing_segments_are_not_archive_routes(self):
        req = urllib.request.Request(
            self._url("/v1/projects/proja/archive/../evil"),
            method="GET",
            headers={"X-ACB-Token": self._token()},
        )
        self.assertEqual(self._http_error(req), 404)

    # ------------------------------------------------------------- concurrency

    def test_concurrent_ensure_produces_one_valid_archive(self):
        results: list[dict] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def run():
            try:
                barrier.wait(timeout=5)
                with self._post_ensure("proja") as resp:
                    results.append(json.loads(resp.read().decode("utf-8")))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 2)
        archives = list(self.archive_dir.glob("*.zip"))
        self.assertEqual(len(archives), 1, "concurrent ensure must not leave partial archives")
        final = archives[0]
        self.assertTrue(final.is_file())
        self.assertGreater(final.stat().st_size, 0)
        with zipfile.ZipFile(final) as bundle:
            self.assertIsNone(bundle.testzip())
        self.assertIn(_sha256(final), {item["sha256"] for item in results})
        self.assertTrue(any(item["packed"] for item in results))

    # ------------------------------------------------- P1 TARGET A / C / D

    def _get_projects(self) -> dict:
        req = urllib.request.Request(
            self._url("/v1/projects"), method="GET", headers={"X-ACB-Token": self._token()}
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _counted_hasher(self):
        """`_sha256_path` with a call counter, so 'did this reread the ZIP?' is
        answered by evidence instead of by a timing guess."""
        import unittest.mock as mock

        original = AudapackBridgeHandler.__dict__["_sha256_path"]
        calls = {"count": 0}

        def counting(path):
            calls["count"] += 1
            return original.__func__(path)

        patcher = mock.patch.object(AudapackBridgeHandler, "_sha256_path", staticmethod(counting))
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_ensure_reports_the_bounded_phase_timings(self):
        """P1 TARGET A: the dominant phase must be measurable from the response."""
        with self._post_ensure("proja") as resp:
            first = json.loads(resp.read().decode("utf-8"))
        timings = first["timings"]
        for key in (
            "ensure_total_ms",
            "freshness_probe_ms",
            "server_archive_sha_ms",
            "hot_proof",
            "source_walk_skipped",
            "sha_receipt_reused",
        ):
            self.assertIn(key, timings)
        self.assertGreaterEqual(timings["ensure_total_ms"], 0.0)
        self.assertGreaterEqual(timings["freshness_probe_ms"], 0.0)
        self.assertGreaterEqual(timings["server_archive_sha_ms"], 0.0)
        self.assertIn(first["sha_source"], {"computed", "receipt"})
        self.assertEqual(first["sha256"], _sha256(self.archive_dir / first["filename"]))

    def test_a_reused_ensure_does_not_reread_the_zip_for_its_digest(self):
        """P1 TARGET D: REUSED_EXISTING must not hash the archive again."""
        with self._post_ensure("proja") as resp:
            first = json.loads(resp.read().decode("utf-8"))

        calls = self._counted_hasher()
        with self._post_ensure("proja") as resp:
            second = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(second["sha256"], first["sha256"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["sha_source"], "receipt")
        self.assertTrue(second["timings"]["sha_receipt_reused"])
        self.assertEqual(calls["count"], 0, "a proven receipt must not trigger a full ZIP read")

    def test_a_changed_archive_invalidates_the_receipt(self):
        """P1 TARGET D: any identity change forces a real hash."""
        with self._post_ensure("proja") as resp:
            first = json.loads(resp.read().decode("utf-8"))
        archive = self.archive_dir / first["filename"]

        # Make the source newer so the next ensure genuinely repacks.
        newer = archive.stat().st_mtime + 10
        target = self.source / "hello.txt"
        os.utime(target, (newer, newer))

        calls = self._counted_hasher()
        with self._post_ensure("proja") as resp:
            second = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(second["packed"])
        self.assertNotEqual(second["sha256"], first["sha256"])
        self.assertEqual(second["sha_source"], "computed")
        self.assertEqual(calls["count"], 1, "new bytes must be hashed exactly once")
        self.assertEqual(second["sha256"], _sha256(self.archive_dir / second["filename"]))

    def test_the_download_route_never_rereads_a_receipted_archive(self):
        """P1 TARGET D on the download path: hashing the whole ZIP before
        streaming it was a second full read of the same bytes."""
        with self._post_ensure("proja") as resp:
            first = json.loads(resp.read().decode("utf-8"))
        archive = self.archive_dir / first["filename"]

        calls = self._counted_hasher()
        with self._get_archive("proja") as resp:
            body = resp.read()
            header = resp.headers.get("X-AUDAPACK-Archive-SHA256")
            source = resp.headers.get("X-AUDAPACK-Archive-SHA256-Source")
        self.assertEqual(body, archive.read_bytes())
        self.assertEqual(header, _sha256(archive))
        self.assertEqual(source, "receipt")
        self.assertEqual(calls["count"], 0)

    def test_the_download_route_reports_bounded_prep_timing_without_paths(self):
        """SRC-083 TARGET C: pre-stream work is attributable from the wire.

        Prep and digest durations ride diagnostic headers plus Server-Timing,
        all exposed to the browser; none of them may carry a filesystem path.
        """
        with self._post_ensure("proja") as resp:
            json.loads(resp.read().decode("utf-8"))
        with self._get_archive("proja") as resp:
            resp.read()
            prep = resp.headers.get("X-AUDAPACK-Archive-Prep-Ms")
            digest = resp.headers.get("X-AUDAPACK-Archive-Digest-Ms")
            timing = resp.headers.get("Server-Timing")
            exposed = resp.headers.get("Access-Control-Expose-Headers")
        self.assertGreaterEqual(float(prep), 0.0)
        self.assertGreaterEqual(float(digest), 0.0)
        self.assertLessEqual(float(digest), float(prep) + 0.001)
        for name in ("auth;dur=", "resolve;dur=", "digest;dur=", "prep;dur="):
            self.assertIn(name, timing)
        for header in ("X-AUDAPACK-Archive-Prep-Ms", "X-AUDAPACK-Archive-Digest-Ms", "Server-Timing"):
            self.assertIn(header, exposed)
        for value in (prep, digest, timing):
            self.assertNotIn(str(self.temp_dir), value)
            self.assertNotIn("\\", value)
            self.assertNotIn("/", value)

    def test_the_transport_probe_streams_bounded_zero_bytes_without_a_token(self):
        """SRC-083 TARGET F: a GM vs native-fetch comparison needs a body of the
        archive's size, but a native fetch must never carry the Bridge token.
        The probe is therefore content-free, token-free and size-clamped."""
        request = urllib.request.Request(self._url("/v1/probe/bytes?size=12345"), method="GET", headers={"Origin": "https://chatgpt.com"})
        with urllib.request.urlopen(request) as resp:
            body = resp.read()
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "https://chatgpt.com")
        self.assertEqual(len(body), 12345)
        self.assertEqual(body.count(0), 12345, "the probe body carries no data")

        with urllib.request.urlopen(self._url("/v1/probe/bytes?size=999999999")) as resp:
            self.assertEqual(int(resp.headers.get("Content-Length")), AudapackBridgeHandler.TRANSPORT_PROBE_MAX_BYTES)
            resp.read()
        with urllib.request.urlopen(self._url("/v1/probe/bytes?size=nope")) as resp:
            self.assertEqual(len(resp.read()), 1 << 20)

    def test_the_registry_revision_is_a_content_digest_not_a_clock(self):
        """P1 TARGET C: the picker may keep a cached list while the CONTENT it
        caches is unchanged. A wall-clock revision could never support that."""
        first = self._get_projects()
        second = self._get_projects()
        self.assertTrue(first["revision"])
        self.assertEqual(first["revision"], second["revision"])

        self.config.projects[0].display_name = "Project A Renamed"
        renamed = self._get_projects()
        self.assertNotEqual(renamed["revision"], first["revision"])
        self.assertIn("Project A Renamed", [p["display_name"] for p in renamed["projects"]])

        self.config.projects[0].enabled = False
        disabled = self._get_projects()
        self.assertNotEqual(disabled["revision"], renamed["revision"])


@unittest.skipUnless(hot_freshness.monitor_is_supported(), "hot proofs need the Windows source watch")
class TestPackSeedsTheHotProof(_BridgeArchiveFixture):
    """T-237: a direct PACK makes the very next unchanged ensure hot.

    Uses the REAL source watch: the proof must come from continuous
    observation, and a real write must take it away again.
    """

    def setUp(self):
        hot_freshness.reset()
        self.addCleanup(hot_freshness.reset)
        super().setUp()

    def _pack_directly(self):
        from audapack.services.packing_service import PackingService

        service = PackingService(self.config, base_dir=str(self.temp_dir))
        result = service.pack_project("proja")
        self.assertTrue(result.success, result.error_message)
        return result

    def _ensure(self) -> dict:
        with self._post_ensure("proja") as resp:
            self.assertEqual(resp.status, 200)
            return json.loads(resp.read().decode("utf-8"))

    def _wait_for_dirty(self, generation: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        key = str(self.source.resolve()).lower()
        while time.monotonic() < deadline:
            for monitor in hot_freshness.diagnostics()["monitors"]:
                if monitor["source_root"] == key and monitor["generation"] > generation:
                    return
            time.sleep(0.02)
        self.fail("the source watch never observed the write")

    def _generation(self) -> int:
        key = str(self.source.resolve()).lower()
        for monitor in hot_freshness.diagnostics()["monitors"]:
            if monitor["source_root"] == key:
                self.assertTrue(monitor["proof"], "the pack must have recorded a proof")
                return monitor["generation"]
        self.fail("the pack did not arm a source watch")

    def test_pack_then_unchanged_ensure_is_hot(self):
        packed = self._pack_directly()
        warm = self._ensure()
        self.assertTrue(warm["ok"])
        self.assertTrue(warm["reused"])
        self.assertFalse(warm["packed"])
        self.assertIs(warm["timings"]["hot_proof"], True)
        self.assertIs(warm["timings"]["source_walk_skipped"], True)
        self.assertEqual(warm["filename"], Path(packed.output_path).name)
        self.assertEqual(warm["sha256"], _sha256(Path(packed.output_path)))

    def test_a_source_edit_after_pack_forces_the_walk_and_a_repack(self):
        self._pack_directly()
        generation = self._generation()
        (self.source / "hello.txt").write_text("hello changed", encoding="utf-8")
        future = time.time() + 10
        os.utime(self.source / "hello.txt", (future, future))
        self._wait_for_dirty(generation)
        changed = self._ensure()
        self.assertTrue(changed["ok"])
        self.assertIs(changed["timings"]["hot_proof"], False)
        self.assertIs(changed["timings"]["source_walk_skipped"], False)
        self.assertTrue(changed["packed"])
        self.assertFalse(changed["reused"])
        with zipfile.ZipFile(self.archive_dir / changed["filename"]) as zf:
            self.assertIn("hello changed", zf.read("hello.txt").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
