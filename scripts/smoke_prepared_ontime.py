"""Explicit Windows smoke: one 2-minute ON TIME job across scheduler restart.

Run with --run only. This sends one tiny real Codex request using Codex 2.
The project and scheduler database live in a temporary directory.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audapack.account_registry import AccountRegistry, discover_accounts
from audapack.config import AppConfig, LauncherConfig
from audapack.limit_adapters import CodexLimitAdapter
from audapack.limits import Availability, LimitStore
from audapack.models import Project
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger
from audapack.prepared_worker import PreparedWorker


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="authorize one real tiny Codex request")
    args = parser.parse_args()
    if not args.run:
        parser.error("pass --run to perform the real CLI request")
    launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    account = next((item for item in discover_accounts(launchers)
                    if item.provider_id == "codex" and "main_codex2" in item.launcher_ids), None)
    if account is None:
        raise SystemExit("Codex 2 account/profile not discovered")
    snapshot = CodexLimitAdapter().probe_limits(account)
    if snapshot.availability() not in (Availability.AVAILABLE, Availability.LOW):
        raise SystemExit(f"Codex 2 is {snapshot.availability().value}; no request sent")
    with tempfile.TemporaryDirectory(prefix="audapack-ontime-") as root:
        folder = Path(root)
        storage = folder / "resources.sqlite3"
        config = AppConfig()
        config.projects = [Project("prepared-smoke", "Prepared smoke", str(folder))]
        config.launchers = launchers
        accounts = AccountRegistry(storage)
        accounts.upsert([account])
        limits = LimitStore(storage)
        jobs = PreparedStore(storage)
        now = datetime.now(timezone.utc)
        due = now + timedelta(minutes=2, seconds=10)
        limits.put(snapshot, due + timedelta(minutes=15), 0)
        job = PreparedJob(
            "prepared-smoke", "one safe delivery", "prepared-smoke", "main_codex2",
            account.account_id, Trigger.ON_TIME, Payload.STATIC_PROMPT,
            {"text": "Return exactly OK. Do not read or write any files."},
            {"at": due.isoformat(), "availability_policy": "AT_TIME_IF_AVAILABLE"},
            effort="high", enabled=True, state=JobState.ARMED,
            created_at=now.isoformat(), updated_at=now.isoformat(),
        )
        jobs.save(job)
        import audapack.prepared_worker as worker_module
        original_discovery = worker_module.discover_accounts
        worker_module.discover_accounts = lambda *_args, **_kwargs: [account]
        try:
            first = PreparedWorker(config, account_registry=accounts,
                                   limit_store=limits, prepared_store=jobs)
            first.start()
            time.sleep(12)
            first.stop()
            print(f"armed={due.isoformat()} restart_before_due=PASS", flush=True)
            second = PreparedWorker(config, account_registry=accounts,
                                    limit_store=limits, prepared_store=jobs)
            second.start()
            deadline = due + timedelta(minutes=3)
            while datetime.now(timezone.utc) < deadline:
                current = jobs.get(job.prepared_id)
                if current and current.state in (JobState.DONE, JobState.FAILED_TERMINAL,
                                                 JobState.MISSED):
                    break
                time.sleep(2)
            second.stop()
            current = jobs.get(job.prepared_id)
            if current is None:
                raise SystemExit("job disappeared")
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(storage)) as db:
                receipts = db.execute(
                    "SELECT state, process_id, delivery_hash FROM prepared_executions "
                    "WHERE prepared_id=?", (job.prepared_id,),
                ).fetchall()
            print(f"state={current.state.value} receipts={len(receipts)} "
                  f"receipt_state={receipts[0][0] if receipts else 'NONE'} "
                  f"pid_recorded={bool(receipts and receipts[0][1])} "
                  f"payload_hash_recorded={bool(receipts and receipts[0][2])}", flush=True)
            return 0 if current.state == JobState.DONE and len(receipts) == 1 else 1
        finally:
            worker_module.discover_accounts = original_discovery


if __name__ == "__main__":
    raise SystemExit(main())
