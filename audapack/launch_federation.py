"""The ONE effective launch inventory: local tools plus canonical SAI accounts.

The defect class this module eliminates: a second, static list of *account*
identities (Codex 1/2/Free, Claude 1/2, Antigravity) kept inside AUDAPACK beside
the canonical SAI Accounts registry, so the operator's account truth lived in two
places and the second one went stale the moment they used the manager.

Authority, once each:

* **SAI Accounts owns** account existence, ``account_id``, provider, display
  name, compact label, global lifecycle state and the execution context. None of
  those is decided here.
* **AUDAPACK owns** what is local to it: whether an account-backed target is
  shown, where it sits in the operator's order, its console colours. That lives
  on the materialised row, never in the registry.

So this module does not introduce a launcher type, a table or a second list. It
RECONCILES ``AppConfig.launchers`` -- the list every surface already reads -- onto
the canonical snapshot, and it materialises an account-backed target as an
ordinary :class:`~audapack.config.LauncherConfig` whose id namespaces the
canonical account id (``sai:claude:profile-dir:09ff...``). Every existing
consumer (project-row buttons, Ctrl+digit shortcuts, the instance monitor,
Limits, Prepared, AUTO, the context menu) therefore reads ONE inventory without
being rewired, and a target id is deterministic, collision-safe, reversible and
independent of display name and list order.

STANDALONE vs FEDERATED, and why the flag is durable: an installation that has
never read an authoritative snapshot keeps exactly the local discovery it always
had. Once one has been read, ``federation.active`` stays true, so a control-plane
outage keeps the last-known-good projection and marks it ``stale`` -- it never
resurrects the retired static account rows, which is the whole point of removing
them. A failed read is not an authoritative empty account list and deletes
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Container, Mapping

from audapack import sai_accounts
from audapack.config import DEFAULT_SHORT_LABELS, AppConfig, LauncherConfig

#: Providers whose accounts AUDAPACK knows how to launch. Class B of the
#: authority model: account-backed launch targets. Providers outside this set
#: stay invisible no matter what the registry holds.
PROVIDERS: tuple[str, ...] = ("codex", "claude", "antigravity")

#: The retired static account-backed launcher identities. They are migration
#: aliases now, never account truth. ``opencode``/``freebuff``/``cline``/``zcode``
#: are deliberately absent: those are Class A local tool launchers and stay
#: AUDAPACK-owned.
ACCOUNT_BACKED_LEGACY_IDS: tuple[str, ...] = (
    "main_codex",
    "main_codex2",
    "main_codex3_free",
    "claude1",
    "claude2",
    "antigravity",
)

#: One namespaced id per canonical account. The prefix says out loud that the
#: identity behind this row is the registry's, not AUDAPACK's.
ACCOUNT_TARGET_PREFIX = "sai:"


def target_id(account_id: str) -> str:
    """The launcher id for a canonical account id."""
    value = (account_id or "").strip()
    return f"{ACCOUNT_TARGET_PREFIX}{value}" if value else ""


def account_id_of(launcher_id: str) -> str:
    """The canonical account id behind a launcher id, or "" for a local tool."""
    value = str(launcher_id or "")
    if not value.startswith(ACCOUNT_TARGET_PREFIX):
        return ""
    return value[len(ACCOUNT_TARGET_PREFIX):]


def is_account_target(launcher_id: str) -> bool:
    return bool(account_id_of(launcher_id))


@dataclass(frozen=True)
class FederationSync:
    """What one reconciliation pass did, in facts a caller can render.

    ``state`` is ``standalone`` (no authoritative snapshot has ever been read),
    ``federated`` (this pass read one, and the inventory is current) or ``stale``
    (this installation federated before and this pass could not read the plane).
    """

    state: str
    fresh: bool
    generation: int = 0
    last_sync: str = ""
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    migrated: tuple[str, ...] = field(default=())
    unchanged: bool = True


def acquire_listing(providers: Container[str] = PROVIDERS, timeout: float | None = None) -> sai_accounts.SharedListing:
    """Acquire one immutable authority snapshot from the control plane."""
    return sai_accounts.snapshot(providers, timeout=timeout)


def reconcile_listing(config: AppConfig, listing: sai_accounts.SharedListing, *,
                      home: Path | None = None, now: datetime | None = None) -> FederationSync:
    """Reconcile an AppConfig onto an already-acquired SharedListing."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    if not listing.ok:
        return _unavailable(config, "control plane did not answer")
    return _reconcile(config, listing, home=home, stamp=stamp)


def sync_effective_launchers(config: AppConfig, *, home: Path | None = None,
                             now: datetime | None = None) -> FederationSync:
    """Read the control plane once and reconcile the effective inventory onto it.

    Deliberately NOT called anywhere implicitly: one owner per process decides
    when a refresh happens, so no widget can spawn its own ``sai-accounts list``.
    """
    if not sai_accounts.engine_path():
        # No plane on this machine. Never federate one that has no registry, and
        # never mutate a projection this pass could not check.
        return _unavailable(config, "no control plane installed")
    listing = acquire_listing(PROVIDERS)
    return reconcile_listing(config, listing, home=home, now=now)


def _unavailable(config: AppConfig, _why: str) -> FederationSync:
    config.federation.stale = bool(config.federation.active)
    return FederationSync(
        state="stale" if config.federation.active else "standalone",
        fresh=False,
        generation=config.federation.generation,
        last_sync=config.federation.last_sync,
    )


def _reconcile(config: AppConfig, listing: sai_accounts.SharedListing, *,
               home: Path | None, stamp: str) -> FederationSync:
    """Materialise one launcher row per canonical account, and retire the rest.

    The plan is built in one pass over the EXISTING list so the operator's order
    survives: a retired legacy row's slot is where its canonical account is
    materialised. What happens to a row, in full:

    * an account-backed row whose account is no longer in the snapshot is
      REMOVED -- a withdrawn account must not stay executable;
    * a pristine historical built-in row is retired, and its local presentation
      (visibility, short label, console colours, position) moves onto the
      canonical row it provably identified;
    * a historical row the operator customised (a ``command_template``) is kept
      untouched as a LOCAL CUSTOM launcher: that is their work, not ours;
    * a local tool row is never touched.
    """
    if not listing.ok:
        return _unavailable(config, "control plane snapshot non-authoritative")
    home = Path(home) if home is not None else Path.home()
    canonical = [entry for entry in listing.accounts if entry.account_id]
    seen_accounts = set()
    for entry in canonical:
        if entry.account_id in seen_accounts:
            return _unavailable(config, "duplicate canonical account_id in listing")
        seen_accounts.add(entry.account_id)
    # Keyed by the canonical account_id: that is the identity, and the ``sai:``
    # id is only one of its projections.
    targets = {entry.account_id: entry for entry in canonical}
    target_ids = {target_id(account_id) for account_id in targets}
    donors = legacy_donors(home)
    labels = _compact_labels(canonical)
    present = {account_id_of(lc.id) for lc in config.launchers
               if account_id_of(lc.id) in targets}

    plan: list[tuple[str, object]] = []
    donor_by_account: dict[str, LauncherConfig] = {}
    migrated: list[str] = []
    removed: list[str] = []
    for launcher in config.launchers:
        if is_account_target(launcher.id):
            if launcher.id in target_ids:
                plan.append(("keep", launcher))
            else:
                removed.append(launcher.id)
            continue
        if launcher.id in ACCOUNT_BACKED_LEGACY_IDS:
            if str(launcher.command_template or "").strip():
                plan.append(("keep", launcher))
                continue
            removed.append(launcher.id)
            migrated.append(launcher.id)
            account_id = donors.get(launcher.id, "")
            # One canonical account, one row: if it already has a row, this
            # retired alias leaves no slot behind.
            if account_id and account_id not in present:
                plan.append(("slot", account_id))
                donor_by_account.setdefault(account_id, launcher)
            continue
        plan.append(("keep", launcher))

    added: list[str] = []
    mutated = False
    material: dict[str, LauncherConfig] = {}
    for account_id, entry in targets.items():
        if account_id in present:
            if _refresh_presentation(entry, next(
                    (row for row in config.launchers if row.id == target_id(account_id)), None)):
                mutated = True
            continue
        row = _account_row(entry, donor_by_account.get(account_id), labels)
        material[account_id] = row
        added.append(row.id)

    out: list[LauncherConfig] = []
    for kind, item in plan:
        if kind == "keep":
            out.append(item)          # type: ignore[arg-type]
            continue
        account_id = str(item)
        row = material.pop(account_id, None)   # a slot without a row leaves nothing
        if row is not None:
            out.append(row)
    out.extend(material.values())
    config.launchers = out

    _repoint_double_click(config, donors, target_ids)
    config.federation.active = True
    config.federation.stale = False
    config.federation.generation = listing.generation
    config.federation.last_sync = stamp
    return FederationSync(
        state="federated",
        fresh=True,
        generation=listing.generation,
        last_sync=stamp,
        added=tuple(added),
        removed=tuple(removed),
        migrated=tuple(migrated),
        unchanged=not (added or removed or mutated),
    )


def legacy_donors(home: Path) -> dict[str, str]:
    """legacy launcher id -> the canonical account id the plane proves it is.

    The mapping is not a second table: it re-derives each local identity through
    the same ``local_candidates`` table and the same ``identity_locator`` the
    registry uses, so a legacy alias can only ever map onto the account that
    genuinely shares its locator. An unmapped alias migrates nothing.
    """
    from audapack.account_registry import identity_locator, local_candidates

    donors: dict[str, str] = {}
    for provider, launcher_id, _label, profile in local_candidates(home):
        locator = identity_locator(provider, profile, home)
        account_id = sai_accounts.canonical_id_for(provider, locator)
        if account_id:
            donors[launcher_id] = account_id
    return donors


def _compact_labels(canonical: list[sai_accounts.SharedAccount]) -> dict[str, str]:
    """One short label per account: the registry's own, disambiguated if it collides.

    The canonical compact label is the DEFAULT, never a second hard-coded A1/C1
    table. Two accounts may legitimately carry the same compact label, and a
    collision must not silently merge identities -- the label is bumped instead,
    while identity stays the canonical ``account_id``.
    """
    used: dict[str, int] = {}
    labels: dict[str, str] = {}
    for entry in canonical:
        base = (entry.compact_label or entry.display_name or entry.provider_id).strip().upper()[:4]
        base = base or "??"
        count = used.get(base, 0) + 1
        used[base] = count
        labels[target_id(entry.account_id)] = base if count == 1 else f"{base}{count}"
    return labels


def _account_row(entry: sai_accounts.SharedAccount, donor: LauncherConfig | None,
                 labels: dict[str, str]) -> LauncherConfig:
    """A canonical account as an ordinary launcher row.

    Everything the registry owns is written from the snapshot and is re-derived
    on every sync; everything local (visibility, colours, the operator's own
    short-label override, capacity) is inherited from the row this account used
    to be, so a migration never asks the operator to set it up again.
    """
    row = LauncherConfig(
        id=target_id(entry.account_id),
        name=entry.display_name or entry.account_id,
        short_label=labels.get(target_id(entry.account_id), "") or entry.provider_id[:2].upper(),
        command_template="",
        agent_type=str(getattr(donor, "agent_type", "") or "powershell"),
        enabled=bool(getattr(donor, "enabled", True)),
        max_instances=int(getattr(donor, "max_instances", 0) or 0),
        console_background_color=str(getattr(donor, "console_background_color", "") or ""),
        console_foreground_color=str(getattr(donor, "console_foreground_color", "") or ""),
    )
    if donor is not None and str(donor.short_label or "").strip() and \
            donor.short_label.strip() != DEFAULT_SHORT_LABELS.get(donor.id, ""):
        # A local short-label override the operator made is presentation, not
        # account truth, so it travels with the account it described.
        row.short_label = donor.short_label.strip()
    _apply_execution(row, entry)
    return row


def _refresh_presentation(entry: sai_accounts.SharedAccount, row: LauncherConfig | None) -> bool:
    """Keep an existing account row describing the account it points at.

    The registry owns the display name, so a rename in the manager reaches the
    button. The short label is NOT overwritten: it is locally editable, and a
    refresh that clobbered an operator's own label every few minutes would be
    the opposite of a projection.
    """
    if row is None:
        return False
    before = (
        row.name,
        row.resolved_command,
        row.resolved_probe,
        row.resolved_profile,
        row.resolved_stage,
        row.resolved_is_cli,
    )
    if entry.display_name and row.name != entry.display_name:
        row.name = entry.display_name
    _apply_execution(row, entry)
    after = (
        row.name,
        row.resolved_command,
        row.resolved_probe,
        row.resolved_profile,
        row.resolved_stage,
        row.resolved_is_cli,
    )
    return before != after


def _apply_execution(row: LauncherConfig, entry: sai_accounts.SharedAccount) -> None:
    """Write the runtime-owned invocation for one account target, in place.

    An account the plane exposes no usable execution context for keeps an EMPTY
    resolution, so the row is honestly unavailable and no click can start the
    wrong profile. The canonical ``account_id`` is handed to the adapter because
    a session-owned account (``windows_user``) is launched by the PLANE through
    its own contract, and the plane needs the identity it owns -- never a
    profile directory this machine happens to have current.
    ``command_template`` is never written: it is the operator's, and the launch
    authority refuses a templated account target on purpose.
    """
    from audapack.cli_launchers import account_launch_resolution

    resolution = account_launch_resolution(entry.provider_id, entry.backend,
                                           entry.locator, account_id=entry.account_id)
    if not resolution.ok:
        row.resolved_command = ""
        row.resolved_probe = ""
        row.resolved_profile = ""
        row.resolved_stage = ""
        row.resolved_is_cli = True
        return
    row.resolved_command = resolution.raw_command or resolution.console_command
    row.resolved_probe = resolution.probe
    row.resolved_profile = resolution.profile
    row.resolved_stage = resolution.stage
    row.resolved_is_cli = bool(resolution.is_cli)


def _repoint_double_click(config: AppConfig, donors: Mapping[str, str],
                          target_ids: Container[str]) -> None:
    """A project double-click that named a retired alias names its account now."""
    current = str(getattr(config.ui, "project_double_click_launcher_id", "") or "")
    account_id = donors.get(current, "")
    if account_id and target_id(account_id) in target_ids:
        config.ui.project_double_click_launcher_id = target_id(account_id)


# ── persisted Prepared/Scheduler bindings ───────────────────────────────────

#: A persisted row's binding already names its canonical account.
OK = "OK"
#: The binding named a retired static id and provably resolved onto exactly one
#: canonical account that the plane still proves. The row is re-pointed.
MIGRATED = "MIGRATED"
#: The binding cannot be resolved to ONE canonical account. The row is BLOCKED
#: and never runs: picking a different account of the same provider would be a
#: wrong-account launch, which is worse than a job that waits for the operator.
ACCOUNT_MAPPING_REQUIRED = "ACCOUNT_MAPPING_REQUIRED"

#: Bindings that are not account bindings at all and are never rewritten.
_BINDING_SENTINELS = frozenset({"", "AUTO", "SYNC"})

@dataclass(frozen=True)
class PreparedBinding:
    """What a persisted ``(account_id, launcher_id)`` pair amounts to now."""

    state: str
    account_id: str = ""
    launcher_id: str = ""
    reason: str = ""

    @property
    def runnable(self) -> bool:
        return self.state in (OK, MIGRATED)

def resolve_prepared_binding(account_id: str, launcher_id: str, *,
                             donors: Mapping[str, str],
                             proven: Container[str] | None = None) -> PreparedBinding:
    """Resolve ONE persisted binding, or refuse to guess.

    ``donors`` maps a retired legacy launcher id to the canonical account id the
    plane proves it is (``legacy_donors``), and ``proven`` is the set of
    canonical account ids the current snapshot actually holds. Both are supplied
    by the caller, so this stays pure and no second ``sai-accounts list`` is
    ever spawned from here.

    ``proven=None`` means NO authoritative snapshot was read this pass (the
    plane is absent or did not answer). Then nothing is decided at all: a failed
    read is not an authoritative empty account list, so it may neither re-point
    nor block a job. An authoritative snapshot that simply does not hold the
    account (``proven=set()``) is a different answer, and that one blocks.

    The three outcomes are deliberate, and the middle one is why this exists:
    a retired id either maps onto the ONE account that shares its proven
    identity, or the row is BLOCKED with ``ACCOUNT_MAPPING_REQUIRED``. It is
    never re-pointed at another account of the same provider, and a provider
    match is never treated as an identity match.
    """
    account_id = str(account_id or "")
    launcher_id = str(launcher_id or "")
    if account_id in _BINDING_SENTINELS or launcher_id in _BINDING_SENTINELS:
        return PreparedBinding(OK, account_id, launcher_id)
    account_backed = (is_account_target(launcher_id)
                      or launcher_id in ACCOUNT_BACKED_LEGACY_IDS
                      or account_id in ACCOUNT_BACKED_LEGACY_IDS)
    if not account_backed:
        # A Class A local tool launcher: AUDAPACK owns it and the registry has
        # no opinion about it.
        return PreparedBinding(OK, account_id, launcher_id)
    if proven is None:
        return PreparedBinding(OK, account_id, launcher_id,
                               reason="no authoritative snapshot this pass")

    if is_account_target(launcher_id):
        named = account_id_of(launcher_id)
        if named == account_id:
            return PreparedBinding(OK, account_id, launcher_id)
        return PreparedBinding(
            ACCOUNT_MAPPING_REQUIRED, reason=(
                "the row names launcher %s, whose account is %s, but the job's "
                "account is %s" % (launcher_id, named, account_id or "empty")))

    # A retired static id. Every legacy id the row names has to be accounted
    # for: two aliases resolving to different accounts are an ambiguity, not a
    # majority vote, and a pair where only one is provable is not half-migrated
    # on the strength of the half that happened to resolve.
    legacy_ids = [value for value in (account_id, launcher_id)
                  if value in ACCOUNT_BACKED_LEGACY_IDS]
    unprovable = [value for value in legacy_ids if not donors.get(value)]
    if unprovable:
        return PreparedBinding(
            ACCOUNT_MAPPING_REQUIRED, reason=(
                "no canonical account proves the identity %s" % unprovable[0]))
    resolved = {donors[value] for value in legacy_ids}
    if len(resolved) > 1:
        return PreparedBinding(
            ACCOUNT_MAPPING_REQUIRED, reason=(
                "the row names two identities (%s, %s) that resolve to different "
                "canonical accounts" % (account_id, launcher_id)))
    if not resolved:
        return PreparedBinding(
            ACCOUNT_MAPPING_REQUIRED, reason="the row names no resolvable identity")
    canonical = next(iter(resolved))
    if account_id not in ACCOUNT_BACKED_LEGACY_IDS and account_id != canonical:
        # The row already named an account, and its launcher proves a different
        # one. Re-pointing either way would be choosing for the operator.
        return PreparedBinding(
            ACCOUNT_MAPPING_REQUIRED, reason=(
                "the row names account %s but its launcher resolves to %s"
                % (account_id or "empty", canonical)))
    if canonical not in proven:
        return PreparedBinding(
            ACCOUNT_MAPPING_REQUIRED, reason=(
                "canonical account %s is not in the current registry snapshot "
                "(withdrawn, or hidden by lifecycle)" % canonical))
    return PreparedBinding(MIGRATED, canonical, target_id(canonical))

@dataclass(frozen=True)
class BindingMigration:
    """What one migration pass did, in facts a caller can render."""

    migrated: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    unchanged: int = 0

def migrate_prepared_bindings(store, *, donors: Mapping[str, str],
                              proven: Container[str] | None = None) -> BindingMigration:

    """Re-point or block every persisted row that still names a retired id.

    Additive and idempotent: it rewrites only the binding of a row that resolves
    unambiguously, annotates an unresolvable row as BLOCKED without touching its
    stored ids, and deletes nothing -- a historical Prepared or audit record is
    evidence, not garbage. ``proven=None`` (no authoritative snapshot this pass)
    changes nothing at all, so a control-plane outage can neither block nor
    re-point a job; only an authoritative snapshot that lacks the account does.
    """
    from dataclasses import replace

    from audapack.prepared import TERMINAL_STATES, JobState, Trigger

    migrated: list[str] = []
    blocked: list[str] = []
    unchanged = 0
    for job in list(store.list()):
        if job.state in TERMINAL_STATES:
            unchanged += 1
            continue

        binding = resolve_prepared_binding(job.account_id, job.launcher_id,
                                           donors=donors, proven=proven)
        if binding.state == ACCOUNT_MAPPING_REQUIRED:
            reason = "%s: %s" % (ACCOUNT_MAPPING_REQUIRED, binding.reason)
            if job.state == _blocked_state() and job.waiting_reason == reason:
                unchanged += 1
                continue
            blocked.append(job.prepared_id)
            store.save(block_job(job, reason))
            continue

        sync_members = []
        if (job.trigger == Trigger.SYNC or getattr(job, "account_id", "") == "SYNC") and hasattr(store, "get_sync_members"):
            sync_members = store.get_sync_members(job.prepared_id)

        sync_blocked = False
        sync_migrated = False
        if sync_members:
            updated_members = []
            for m in sync_members:
                mbinding = resolve_prepared_binding(m.account_id, m.launcher_id,
                                                    donors=donors, proven=proven)
                if mbinding.state == ACCOUNT_MAPPING_REQUIRED:
                    reason = "%s: member %d (%s): %s" % (
                        ACCOUNT_MAPPING_REQUIRED, m.member_index,
                        m.account_id or m.launcher_id, mbinding.reason
                    )
                    if job.state == _blocked_state() and job.waiting_reason == reason:
                        sync_blocked = True
                        break
                    blocked.append(job.prepared_id)
                    store.save(block_job(job, reason))
                    sync_blocked = True
                    break
                elif mbinding.state == MIGRATED:
                    if (m.account_id, m.launcher_id) != (mbinding.account_id, mbinding.launcher_id):
                        sync_migrated = True
                        updated_members.append(replace(m, account_id=mbinding.account_id,
                                                       launcher_id=mbinding.launcher_id))
                    else:
                        updated_members.append(m)
                else:
                    updated_members.append(m)

            if sync_blocked:
                if job.prepared_id not in blocked:
                    unchanged += 1
                continue

            if sync_migrated and hasattr(store, "save_sync_members"):
                store.save_sync_members(job.prepared_id, updated_members)

        job_migrated = False
        if binding.state == MIGRATED:
            if (job.account_id, job.launcher_id) != (binding.account_id, binding.launcher_id):
                job_migrated = True
                job = rebind_job(job, binding)
                store.save(job)

        if sync_migrated or job_migrated:
            if job.state == _blocked_state() and job.enabled:
                job = replace(job, state=JobState.ARMED, waiting_reason="")
                store.save(job)
            migrated.append(job.prepared_id)
        else:
            unchanged += 1
    return BindingMigration(tuple(migrated), tuple(blocked), unchanged)

def rebind_job(job, binding: PreparedBinding):
    from dataclasses import replace

    from audapack.prepared import JobState

    state = JobState.ARMED if (job.state == JobState.BLOCKED and job.enabled) else job.state
    return replace(job, account_id=binding.account_id,
                   launcher_id=binding.launcher_id, state=state,
                   waiting_reason="" if state != job.state else job.waiting_reason,
                   updated_at=datetime.now(timezone.utc).astimezone(timezone.utc).isoformat())

def _blocked_state():
    from audapack.prepared import JobState

    return JobState.BLOCKED

def block_job(job, reason: str):
    """Annotate a row BLOCKED. Its stored binding is left readable as evidence."""
    from dataclasses import replace

    return replace(job, state=_blocked_state(), waiting_reason=reason)

