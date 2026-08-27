"""Synchronize canonical Python audit profiles into embedded Widget definitions.

Usage:
    python tools/sync_audit_profiles.py [--check]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Ensure root is in sys.path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from audapack.campaign import (  # noqa: E402
    compute_manifest_hash,
    validate_profile_manifest,
)

BEGIN_MARKER = "// BEGIN_EMBEDDED_AUDIT_PROFILES"
END_MARKER = "// END_EMBEDDED_AUDIT_PROFILES"


def generate_embedded_js_block(manifest_data: dict, manifest_hash: str) -> str:
    js_json = json.dumps(manifest_data, indent=2, ensure_ascii=False)
    lines = [
        BEGIN_MARKER,
        f"  const AUDIT_PROFILES_MANIFEST_SHA256 = '{manifest_hash}';",
        f"  const EMBEDDED_AUDIT_PROFILES = Object.freeze({js_json});",
        f"  {END_MARKER}",
    ]
    return "\n".join(lines)


def sync_widget_profiles(check_only: bool = False) -> int:
    root = Path(__file__).resolve().parent.parent
    manifest_path = root / "audapack" / "data" / "audit_profiles.json"
    widget_path = root / "resources" / "AUDAPACK_WIDGET.user.js"

    if not manifest_path.exists():
        print(f"Error: Manifest not found at {manifest_path}", file=sys.stderr)
        return 1
    if not widget_path.exists():
        print(f"Error: Widget not found at {widget_path}", file=sys.stderr)
        return 1

    raw_manifest = manifest_path.read_text(encoding="utf-8")
    manifest_data = json.loads(raw_manifest)
    valid, err = validate_profile_manifest(manifest_data)
    if not valid:
        print(f"Error: Manifest validation failed: {err}", file=sys.stderr)
        return 1

    manifest_hash = compute_manifest_hash(manifest_data)
    embedded_block = generate_embedded_js_block(manifest_data, manifest_hash)

    widget_text = widget_path.read_text(encoding="utf-8")

    pattern = re.compile(
        rf"{re.escape(BEGIN_MARKER)}.*?{re.escape(END_MARKER)}",
        re.DOTALL,
    )

    if pattern.search(widget_text):
        new_widget_text = pattern.sub(lambda _: embedded_block, widget_text)
    else:
        # Insert after BRIDGE_API_VERSION or AUTO_SEND_RECEIPT_PREFIX constants
        anchor = "  const BRIDGE_API_VERSION = "
        idx = widget_text.find(anchor)
        if idx != -1:
            line_end = widget_text.find("\n", idx)
            new_widget_text = (
                widget_text[:line_end + 1]
                + "\n"
                + embedded_block
                + "\n"
                + widget_text[line_end + 1:]
            )
        else:
            new_widget_text = embedded_block + "\n\n" + widget_text

    if widget_text == new_widget_text:
        print(f"Profiles are up to date (hash: {manifest_hash})")
        return 0

    if check_only:
        print("Error: Widget embedded audit profiles are out of sync with audit_profiles.json", file=sys.stderr)
        print("Run 'python tools/sync_audit_profiles.py' to update.", file=sys.stderr)
        return 1

    widget_path.write_text(new_widget_text, encoding="utf-8")
    print(f"Successfully synced audit profiles to {widget_path} (hash: {manifest_hash})")
    return 0


if __name__ == "__main__":
    check_mode = "--check" in sys.argv
    sys.exit(sync_widget_profiles(check_only=check_mode))
