"""Bounded machine-verifiable widget update probe (T-196 Target D).

WHAT THIS PROVES, AND WHAT IT CANNOT.

Tampermonkey itself does not expose a scriptable update API, so "click Update in
the dashboard and watch the installed script change" stays an operator step.
What CAN be verified mechanically is the part the defect actually lived in:
whether the Bridge is offering bytes that a userscript manager would install as
a NEWER version through the supported delivery path (`@version` comparison at
`@updateURL`/`@downloadURL`).

This probe therefore answers, from the real endpoint:

  1. which version the Bridge serves;
  2. whether that version is STRICTLY GREATER than the version an installed
     Tampermonkey script reports (the only comparison the manager makes);
  3. whether the served `@updateURL`/`@downloadURL` point at this Bridge, which
     is what makes the update reachable at all;
  4. whether the served bytes are the committed release: byte-identical to the
     bundled file when the Bridge answers on its canonical authority, and
     identical everywhere except the two rewritten directives when it answers
     on a configured one (CORE-001: the served script follows the port it was
     fetched from).

It prints a PASS/FAIL receipt and, with `--out`, writes it as JSON so an
acceptance run leaves durable evidence. The remaining operator step -- the
dashboard actually replacing the script -- is recorded in
`docs/AUDAPACK_WIDGET_ACCEPTANCE.md`.

Usage:
    python scripts/widget_update_probe.py --installed-version 0.0.59
    python scripts/widget_update_probe.py --installed-version 0.0.59 --out probe.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audapack.components.widget import (  # noqa: E402
    get_bundled_widget_path,
    read_widget_release,
    widget_version_key,
)

_DIRECTIVE = re.compile(r"(?m)^//\s*@(?:updateURL|downloadURL)\s+\S+\s*$")
_VERSION = re.compile(r"//\s*@version\s+([^\r\n]+)")


def version_key(value: str) -> tuple:
    """Delegate to the ONE canonical comparator (T-196 TARGET D).

    A served version outside the numeric contract cannot be ordered, so an
    unparsable value sorts as the empty tuple -- strictly below any real
    version, which makes the probe report "not newer" (a refusal) instead of
    crashing or silently treating a prerelease suffix as equal.
    """
    try:
        return widget_version_key(value)
    except ValueError:
        return ()


def fetch(url: str, timeout: float = 10.0) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), error.read()
    except OSError:
        return 0, {}, b""


def _without_directives(body: bytes) -> bytes:
    """The served/released script with the two rewritable lines removed."""
    return _DIRECTIVE.sub("", body.decode("utf-8", errors="replace")).encode("utf-8")


def probe(bridge_url: str, installed_version: str | None) -> dict:
    endpoint = bridge_url.rstrip("/") + "/widget.user.js"
    status, headers, body = fetch(endpoint)
    result: dict = {
        "endpoint": endpoint,
        "http_status": status,
        "content_type": headers.get("Content-Type"),
        "sha256": None,
        "served_version": None,
        "installed_version": installed_version,
        "update_url": None,
        "download_url": None,
        "ledger_version": None,
        "ledger_sha256": None,
        "checks": {},
        "verdict": "FAIL",
    }
    if status != 200 or not body:
        result["checks"]["http_200"] = False
        return result
    result["checks"]["http_200"] = True

    text = body.decode("utf-8", errors="replace")
    result["sha256"] = hashlib.sha256(body).hexdigest()
    version_match = _VERSION.search(text)
    if version_match:
        result["served_version"] = version_match.group(1).strip()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("// @updateURL"):
            result["update_url"] = stripped.split()[-1]
        elif stripped.startswith("// @downloadURL"):
            result["download_url"] = stripped.split()[-1]

    result["checks"]["content_type_is_javascript"] = str(headers.get("Content-Type", "")).startswith(
        "text/javascript"
    )
    result["checks"]["served_declares_version"] = bool(result["served_version"])
    directives = [result["update_url"], result["download_url"]]
    result["checks"]["directives_present"] = all(directives)
    result["checks"]["directives_point_at_this_bridge"] = all(
        value and value.rstrip("/").endswith(endpoint) for value in directives
    )

    ledger = read_widget_release() or {}
    result["ledger_version"] = ledger.get("version")
    result["ledger_sha256"] = ledger.get("sha256")
    bundled = get_bundled_widget_path()
    bundled_body = bundled.read_bytes() if bundled.is_file() else b""
    result["bundled_sha256"] = hashlib.sha256(bundled_body).hexdigest() if bundled_body else None
    exact = bool(bundled_body) and bundled_body == body
    result["checks"]["served_bytes_are_the_bundled_file"] = exact
    # The only documented difference between the shipped file and the served
    # bytes is the rewritten endpoint pair, so that is what is compared when
    # they are not identical.
    result["checks"]["served_bytes_match_recorded_release"] = bool(bundled_body) and (
        exact or _without_directives(bundled_body) == _without_directives(body)
    )
    result["checks"]["served_version_matches_release"] = (
        bool(ledger.get("version")) and ledger["version"] == result["served_version"]
    )

    if installed_version:
        result["checks"]["served_version_is_newer"] = version_key(
            result["served_version"] or ""
        ) > version_key(installed_version)
    else:
        result["checks"]["served_version_is_newer"] = None

    required = [
        "http_200",
        "content_type_is_javascript",
        "served_declares_version",
        "directives_present",
        "directives_point_at_this_bridge",
        "served_bytes_match_recorded_release",
        "served_version_matches_release",
    ]
    if installed_version:
        required.append("served_version_is_newer")
    result["verdict"] = "PASS" if all(result["checks"].get(name) for name in required) else "FAIL"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bridge-url",
        default="http://127.0.0.1:17843",
        help="Bridge base URL (default: the canonical endpoint)",
    )
    parser.add_argument(
        "--installed-version",
        default=None,
        help="version an installed Tampermonkey script reports (dashboard)",
    )
    parser.add_argument("--out", default=None, help="write the JSON receipt here")
    args = parser.parse_args(argv)

    result = probe(args.bridge_url, args.installed_version)
    for name, value in result["checks"].items():
        mark = "ok" if value else ("n/a" if value is None else "FAIL")
        print(f"[{mark}] {name}")
    print(
        f"served={result['served_version']} installed={result['installed_version']} "
        f"sha256={result['sha256']}"
    )
    print(f"VERDICT: {result['verdict']}")

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"receipt written: {args.out}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
