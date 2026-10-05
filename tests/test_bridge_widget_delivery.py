"""T-196 Target C: the ACTUAL Bridge-served userscript, not the source file.

The repository can be perfectly consistent and still deliver nothing: an update
reaches an installed browser only through `GET /widget.user.js`, and that route
is not a straight file copy. `_widget_source_for_endpoint` rewrites the two
`@updateURL`/`@downloadURL` directives to the authority the Bridge is actually
being reached on (CORE-001, audit/2.md), so the served bytes and the on-disk
bytes are two different artifacts by design.

These tests exercise the real HTTP endpoint -- the same one an installed
Tampermonkey update check calls -- and assert:

  * byte-for-byte equality with the source file when the requested authority IS
    the canonical one (the rewrite is then a no-op);
  * hash equality with the expected rewritten form for any other authority;
  * a userscript/javascript Content-Type;
  * the NEW `@version` is what the endpoint serves, matching the release ledger;
  * exactly one update and one download directive, pointing at this Bridge.
"""

from __future__ import annotations

import hashlib
import re
import urllib.request
from pathlib import Path

from audapack.components import widget as widget_mod

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "resources" / widget_mod.WIDGET_FILE_NAME

_DIRECTIVE = re.compile(rb"(?m)^//\s*@(updateURL|downloadURL)\s+\S+\s*$")


def _fetch(base_url: str, host_header: str | None = None) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(f"{base_url}/widget.user.js", method="GET")
    if host_header:
        request.add_header("Host", host_header)
    with urllib.request.urlopen(request) as response:
        return response.status, dict(response.headers), response.read()


def test_the_endpoint_serves_the_bundled_userscript_byte_for_byte(bridge_server):
    """With the canonical authority the delivered bytes ARE the shipped bytes."""
    _config, base_url = bridge_server
    status, headers, body = _fetch(base_url, host_header="127.0.0.1:17843")

    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/javascript")
    assert body == SOURCE.read_bytes(), "the served userscript differs from the shipped file"
    assert hashlib.sha256(body).hexdigest() == hashlib.sha256(SOURCE.read_bytes()).hexdigest()


def test_the_served_bytes_have_the_recorded_release_digest(bridge_server):
    """The DELIVERY path carries the recorded release, not just the file."""
    _config, base_url = bridge_server
    _status, _headers, body = _fetch(base_url, host_header="127.0.0.1:17843")
    record = widget_mod.read_widget_release()
    assert hashlib.sha256(body).hexdigest() == record["sha256"]
    assert widget_mod.widget_release_errors() == []


def test_the_new_version_is_what_the_endpoint_serves(bridge_server):
    """A bumped version that never reaches the endpoint is not a delivery."""
    _config, base_url = bridge_server
    _status, _headers, body = _fetch(base_url, host_header="127.0.0.1:17843")
    served_version = re.search(rb"//\s*@version\s+([^\r\n]+)", body)
    assert served_version, "the served userscript declares no // @version"
    assert served_version.group(1).strip().decode() == widget_mod.read_widget_release()["version"]
    assert widget_mod.read_bundled_widget_metadata()["version"] == (
        widget_mod.read_widget_release()["version"]
    )


def test_a_configured_port_rewrites_only_the_two_directives(bridge_server):
    """The documented reason the served bytes differ from the file, bounded."""
    config, base_url = bridge_server
    src = SOURCE.read_bytes()
    _status, _headers, body = _fetch(base_url)

    authority = f"127.0.0.1:{config.bridge.port}"
    expected_directives = [
        f"// @updateURL    http://{authority}/widget.user.js",
        f"// @downloadURL  http://{authority}/widget.user.js",
    ].copy()

    served_directives = [
        line for line in body.split(b"\n")
        if line.startswith((b"// @updateURL", b"// @downloadURL"))
    ]
    assert [line.decode() for line in served_directives] == expected_directives

    # Everything that is NOT a directive line is byte-identical.
    src_rest = [line for line in src.split(b"\n") if not _DIRECTIVE.match(line)]
    served_rest = [line for line in body.split(b"\n") if not _DIRECTIVE.match(line)]
    assert served_rest == src_rest, "the endpoint changed something other than the directives"


def test_the_endpoint_keeps_exactly_one_update_and_one_download_directive(bridge_server):
    """Two copies drift, and Tampermonkey persists whichever it read last."""
    _config, base_url = bridge_server
    _status, headers, body = _fetch(base_url)
    header_block = body.split(b"==/UserScript==")[0]

    assert header_block.count(b"// @updateURL") == 1
    assert header_block.count(b"// @downloadURL") == 1
    for line in header_block.split(b"\n"):
        if line.startswith((b"// @updateURL", b"// @downloadURL")):
            assert line.rstrip().endswith(b"/widget.user.js"), line
    assert headers.get("Content-Length") == str(len(body))


def test_the_endpoint_publishes_the_release_identity_in_status(bridge_server):
    """An operator can see which build the Bridge is handing out."""
    import json

    _config, base_url = bridge_server
    _status, _headers, body = _fetch(base_url)
    record = widget_mod.read_widget_release()

    with urllib.request.urlopen(f"{base_url}/health") as response:
        health = json.loads(response.read().decode("utf-8"))
    assert health["widget_bundle_version"] == record["version"]
    assert record["sha256"].startswith(health["widget_bundle_sha256"])

    _status2, _headers2, body2 = _fetch(base_url)
    assert body2 == body, "the endpoint is not stable across calls"
