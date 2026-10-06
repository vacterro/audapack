"""Summarize real widget delivery timings from the Bridge-mirrored log (SRC-083).

The widget mirrors its diagnostics into ``<runtime>/logs/widget_diagnostics.log``.
Every manual ZIP delivery writes one ``delivery_timing`` record carrying the GET
split, and the diagnostics "Probe GET" button writes one ``transport_probe``
record comparing GM_xmlhttpRequest with native fetch. This script reads those
records so nobody has to copy a status line out of the browser, and names the
dominant GET phase with the classification the latency investigation uses:

    CASE A  time goes before the first byte in the userscript manager
    CASE B  the Bridge itself is slow before streaming
    CASE C  the body transfer through the manager dominates
    CASE D  the GET is fast; materialization or the browser hash dominates
    WARM    same-runtime verified cache hit: zero GET, zero hash

Usage:
    python scripts/widget_timing_report.py              # last 24 hours
    python scripts/widget_timing_report.py --hours 2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audapack.config import get_user_runtime_dir  # noqa: E402


def _records(paths: list[Path], since_ms: float) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and float(entry.get("at") or 0) >= since_ms:
                rows.append(entry)
    rows.sort(key=lambda row: float(row.get("at") or 0))
    return rows


def classify(timing: dict) -> str:
    """The dominant GET phase of one delivery, in the investigation's terms."""
    if int(timing.get("get_count") or 0) == 0:
        return "WARM: zero GET, zero browser hash (verified cache or no-op)"
    onload = float(timing.get("get_total_ms") or 0)
    server = float(timing.get("server_get_prep_ms") or 0)
    headers = float(timing.get("get_headers_ms") or 0)
    first = float(timing.get("get_first_progress_ms") or 0)
    after = float(timing.get("browser_response_materialize_ms") or 0) + float(timing.get("browser_sha_ms") or 0)
    if onload <= 0:
        return "UNMEASURED: no GET split in this record (widget older than 0.0.65?)"
    before_body = first or headers
    if server >= 0.5 * onload:
        return f"CASE B: Bridge pre-stream work {server:.0f} ms of {onload:.0f} ms GET"
    if after > onload:
        return f"CASE D: after-body work {after:.0f} ms (materialize+SHA) exceeds the {onload:.0f} ms GET"
    if before_body and before_body - server >= 0.5 * onload:
        return f"CASE A: {before_body - server:.0f} ms before the first byte reached the userscript (Bridge {server:.0f} ms)"
    if before_body and onload - before_body >= 0.5 * onload:
        return f"CASE C: body transfer {onload - before_body:.0f} ms of {onload:.0f} ms GET"
    if not before_body:
        return f"UNSPLIT: manager reported no headers/progress; GET {onload:.0f} ms total"
    return f"MIXED: before-body {before_body:.0f} ms, body {onload - before_body:.0f} ms"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args(argv)
    logs = Path(get_user_runtime_dir()) / "logs"
    paths = [logs / "widget_diagnostics.log.1", logs / "widget_diagnostics.log"]
    since = (time.time() - args.hours * 3600) * 1000
    rows = _records(paths, since)
    deliveries = [row for row in rows if row.get("event") == "delivery_timing"]
    probes = [row for row in rows if row.get("event") == "transport_probe"]
    if not deliveries and not probes:
        print(f"No delivery_timing or transport_probe records in the last {args.hours:g} h ({logs}).")
        print("Click ZIP once on widget >= 0.0.65 (and Probe GET once), then run this again.")
        return 1
    for row in deliveries:
        when = datetime.fromtimestamp(float(row["at"]) / 1000).strftime("%m-%d %H:%M:%S")
        timing = row.get("timing") or {}
        print(f"{when}  {row.get('message', '')}")
        if timing:
            print(f"    -> {classify(timing)}")
    for row in probes:
        when = datetime.fromtimestamp(float(row["at"]) / 1000).strftime("%m-%d %H:%M:%S")
        print(f"{when}  {row.get('message', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
