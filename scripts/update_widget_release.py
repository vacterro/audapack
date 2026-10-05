"""Record the shipped userscript's release identity (T-196).

`// @version` is the ONLY thing an installed userscript manager compares when it
decides whether its copy is current. Two materially different builds shipped
under one version string are one build as far as every installed browser is
concerned: repository tests exercise the new bytes while the operator's browser
keeps executing the old ones, which is exactly the defect T-196 was filed for.

This script recomputes the bundled userscript's SHA-256 and writes it into the
committed release ledger beside it:

    resources/AUDAPACK_WIDGET.release.json

The recording itself is owned by `audapack.components.widget.record_widget_release`,
which validates the transition BEFORE writing (T-196 TARGET B): the previous
ledger is read first, and different bytes may only be recorded under a strictly
greater numeric `@version`. A same-version rewrite is refused with zero
mutation, so this script can never launder changed bytes under one version.

`audapack.components.widget.widget_release_errors()` is the oracle: it fails
whenever the shipped bytes and the recorded release disagree.

Usage:
    python scripts/update_widget_release.py            # record current bytes
    python scripts/update_widget_release.py --check    # verify, write nothing
    python scripts/update_widget_release.py --bootstrap  # first record only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audapack.components.widget import (  # noqa: E402
    get_widget_release_path,
    read_widget_release,
    record_widget_release,
    widget_release_errors,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the ledger against the shipped bytes and write nothing",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="permit the first record over a missing or malformed ledger",
    )
    args = parser.parse_args(argv)

    if args.check:
        errors = widget_release_errors()
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        if errors:
            return 1
        record = read_widget_release() or {}
        print(f"OK: {record['version']} {record['sha256']}")
        return 0

    result = record_widget_release(bootstrap=args.bootstrap)
    if not result["ok"]:
        print(f"FAIL: {result['code']}: {result['detail']}", file=sys.stderr)
        return 1
    if not result["wrote"]:
        print(f"OK: {result['code']}: {result['detail']}")
    else:
        record = result["record"]
        print(f"recorded {record['version']} {record['sha256']} -> {get_widget_release_path()}")
    errors = widget_release_errors()
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

