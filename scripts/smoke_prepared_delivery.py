"""One explicit, bounded real prepared-delivery smoke in an empty temp project."""

from __future__ import annotations

import argparse
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from audapack.account_registry import AccountRegistry
from audapack.config import AppConfig, LauncherConfig
from audapack.models import Project
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger
from audapack.prepared_delivery import build_launch_plan, execute_claimed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="send one real minimal Codex request")
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required to consume one real provider request")
    account = next((a for a in AccountRegistry().list() if a.display_name == "Codex 2"), None)
    if account is None or "main_codex2" not in account.launcher_ids:
        raise RuntimeError("Codex 2 account is not bound")
    with tempfile.TemporaryDirectory(prefix="audapack-prepared-smoke-") as directory:
        root = Path(directory)
        config = AppConfig()
        config.projects = [Project("prepared-smoke", "Prepared Smoke", str(root))]
        config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
        now = datetime.now(timezone.utc).isoformat()
        job = PreparedJob(
            uuid.uuid4().hex, "safe delivery smoke", "prepared-smoke", "main_codex2",
            account.account_id, Trigger.ON_TIME, Payload.STATIC_PROMPT,
            {"text": "Return exactly AUDAPACK_PREPARED_OK. Do not use tools or edit files."},
            {"at": now}, enabled=True, state=JobState.ARMED,
        )
        store = PreparedStore(root / "receipts.sqlite3")
        store.save(job)
        owner = uuid.uuid4().hex
        execution = store.claim(job.prepared_id, "manual-test-now", owner)
        if execution is None:
            raise RuntimeError("could not claim smoke event")
        plan = build_launch_plan(job, account, config)
        generation = store.receipt(job.prepared_id, "manual-test-now")["claim_generation"]
        result = execute_claimed(plan, job, store, execution, owner, generation,
                                 timeout_seconds=120)
        receipt = store.receipt(job.prepared_id, "manual-test-now")
        print(f"state={result.state.value} exit={result.exit_code} pid={result.process_id}")
        print(f"delivery_sha256={plan.payload_sha256} receipt_state={receipt['state']}")
        return 0 if result.state == JobState.DONE else 1


if __name__ == "__main__":
    raise SystemExit(main())
