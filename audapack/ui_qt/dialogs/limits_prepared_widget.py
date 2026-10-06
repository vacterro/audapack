"""Compact account limits and prepared-job management surface."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QDateTime, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from audapack import sai_accounts
from audapack.account_registry import AccountIdentity, AccountRegistry, discover_accounts
from audapack.limit_adapters import AntigravityLimitAdapter, ClaudeLimitAdapter, CodexLimitAdapter
from audapack.limits import Availability, LimitCoordinator, LimitSnapshot, LimitStore, parse_time
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger
from audapack.prepared_delivery import PreflightError, build_launch_plan, execute_claimed
from audapack.prepared_payloads import inspect_handoff, pin_handoff
from audapack.prepared_sync import PreparedSyncMember
from audapack.provider_capabilities import PROVIDERS


def workflow_gate_reason(trigger: Trigger, payload: Payload,
                         account: AccountIdentity | None, auto: bool) -> str:
    """Exact, honest reason a SYNC/PRIME/AUDIT job cannot be armed yet.

    Mission C5/D3/N: never show a bare "SYNC disabled"; name the missing
    capability (window start semantics, provider support) so the operator sees
    why real synchronization/priming is unavailable. AUDIT is armable once the
    trigger is time-based -- the Bridge worker gates it truthfully at runtime
    (account binding, model/effort, profile, freshness), so a bad time trigger
    is the only editor-side block.
    """
    if payload == Payload.AUDIT:
        if trigger not in (Trigger.ON_TIME, Trigger.ON_RESET):
            return "Audit uses On time or On reset; choose one of those triggers."
        if auto:
            return "Audit cannot use AUTO account selection; choose an explicit bound account."
        return ""
    if trigger in (Trigger.SYNC, Trigger.PRIME):
        label = trigger.value
        if auto:
            return f"{label} cannot use AUTO account selection; choose an explicit bound account."
        capabilities = PROVIDERS.get(account.provider_id) if account else None
        if capabilities is None:
            return f"{label} unavailable: provider capability is unknown for this account."
        supported = (capabilities.supports_sync if trigger == Trigger.SYNC
                     else capabilities.supports_prime)
        semantics = capabilities.window_start_semantics.value
        if not supported:
            return (f"{label} unavailable: {account.provider_id} window start semantics "
                    f"{semantics}; real synchronization needs verified FIRST_CONSUMING_USE.")
        return f"{label} unavailable: worker runtime not connected for {account.provider_id}."
    return ""


def _display_time(value: str | None) -> str:
    moment = parse_time(value)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else "—"


def _meter(windows, kind: str) -> str:
    found = next((window for window in windows if window.kind == kind), None)
    return f"{found.remaining_ratio:.0%}" if found and found.remaining_ratio is not None else "—"


def limit_tooltip(account: AccountIdentity, snapshot: LimitSnapshot | None,
                  next_probe_at: datetime | None = None, next_job: PreparedJob | None = None) -> str:
    lines = [account.display_name, f"Provider: {account.provider_id}",
             f"Launcher: {', '.join(account.launcher_ids) or 'UNBOUND'}"]
    if snapshot is None:
        lines.append("Availability: UNKNOWN")
    else:
        now = datetime.now(timezone.utc)
        observed = parse_time(snapshot.observed_at)
        age = max(0, int((now - observed).total_seconds())) if observed else None
        lines += [f"Availability: {snapshot.availability(now).value}", ""]
        for window in snapshot.windows:
            remaining = (f"{window.remaining_ratio:.0%}" if window.remaining_ratio is not None
                         else str(window.remaining_units) if window.remaining_units is not None else "unknown")
            reset = parse_time(window.reset_at)
            until = max(0, int((reset - now).total_seconds())) if reset else None
            lines += [f"{window.label}: {remaining} remaining",
                      f"Reset: {_display_time(window.reset_at)}" +
                      (f" (in {until // 3600}h {(until % 3600) // 60}m)" if until is not None else "")]
            if window.window_started_at:
                lines.append(f"Window start: {_display_time(window.window_started_at)}")
            lines.append(f"Confidence: {window.confidence}")
        lines += ["", f"Observed: {_display_time(snapshot.observed_at)}",
                  f"Freshness: {age // 60}m old" if age is not None else "Freshness: unknown",
                  f"Source: {snapshot.source}",
                  f"Next probe: {_display_time(next_probe_at.isoformat() if next_probe_at else None)}"]
    if next_job:
        lines += ["", f"Prepared: {next_job.name}", f"Trigger: {next_job.trigger.value}",
                  f"Next launch: {_display_time(next_job.next_due_at)}"]
    return "\n".join(lines)


class PreparedEditor(QDialog):
    def __init__(self, config, accounts: list[AccountIdentity], parent=None, job: PreparedJob | None = None):
        super().__init__(parent)
        self.setWindowTitle("Prepare launch")
        self.config = config
        self.accounts = accounts
        self.job = job
        self.setMinimumWidth(470)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.form = form
        root.addLayout(form)
        self.name = QLineEdit(job.name if job else "")
        form.addRow("Name", self.name)
        self.project = QComboBox()
        for project in config.projects:
            if project.enabled:
                self.project.addItem(project.display_name, project.id)
        form.addRow("Project", self.project)
        self.account = QComboBox()
        self.account.addItem("AUTO (choose an available account at launch)", "AUTO")
        for account in accounts:
            self.account.addItem(f"{account.display_name}  [{', '.join(account.launcher_ids) or 'UNBOUND'}]", account.account_id)
        form.addRow("Account", self.account)
        self.auto_provider = QComboBox()
        self.auto_provider.addItem("Any provider", "")
        for provider_id in sorted({account.provider_id for account in accounts}):
            self.auto_provider.addItem(provider_id, provider_id)
        form.addRow("AUTO provider", self.auto_provider)
        self.sync_members_widget = QWidget()
        sync_layout = QVBoxLayout(self.sync_members_widget)
        sync_layout.setContentsMargins(0, 0, 0, 0)
        self.sync_table = QTableWidget(0, 4)
        self.sync_table.setHorizontalHeaderLabels(["Account", "Launcher", "Model", "Effort"])
        self.sync_table.setMaximumHeight(120)
        sync_layout.addWidget(self.sync_table)
        sync_btns = QHBoxLayout()
        self.add_member_btn = QPushButton("Add member")
        self.remove_member_btn = QPushButton("Remove member")
        sync_btns.addWidget(self.add_member_btn)
        sync_btns.addWidget(self.remove_member_btn)
        sync_layout.addLayout(sync_btns)
        form.addRow("SYNC members", self.sync_members_widget)
        self.add_member_btn.clicked.connect(self._add_sync_member_row)
        self.remove_member_btn.clicked.connect(self._remove_sync_member_row)
        self.trigger = QComboBox()
        for value, label in ((Trigger.ON_RESET, "On reset"), (Trigger.ON_TIME, "On time"),
                             (Trigger.SYNC, "Sync with other account"), (Trigger.PRIME, "Prime window")):
            self.trigger.addItem(label, value.value)
        form.addRow("Trigger", self.trigger)
        self.at = QDateTimeEdit(QDateTime.currentDateTime().addSecs(300))
        self.at.setCalendarPopup(True)
        self.at.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        form.addRow("At local time", self.at)
        self.window = QComboBox()
        self.window.addItem("Next usable", "NEXT_USABLE")
        self.window.addItem("5h", "five_hour")
        self.window.addItem("Weekly", "weekly")
        form.addRow("Reset window", self.window)
        self.policy = QComboBox()
        self.policy.addItem("Wait until available", "AT_TIME_IF_AVAILABLE")
        self.policy.addItem("Strict time", "STRICT_TIME")
        form.addRow("At-time policy", self.policy)
        self.payload = QComboBox()
        for value, label in ((Payload.LATEST_SAIHANDOFF, "Latest SAIHANDOFF"),
                             (Payload.PINNED_SAIHANDOFF, "Pinned SAIHANDOFF"),
                             (Payload.USER_COMMAND, "User command"),
                             (Payload.STATIC_PROMPT, "Static prompt"),
                             (Payload.AUDIT, "Audit")):
            self.payload.addItem(label, value.value)
        form.addRow("Payload", self.payload)
        self.text = QPlainTextEdit()
        self.text.setMaximumHeight(90)
        form.addRow("Command / prompt", self.text)
        self.path = QLineEdit()
        form.addRow("Pinned handoff path", self.path)
        self.audit_profile = QLineEdit()
        form.addRow("Audit profile", self.audit_profile)
        self.model = QLineEdit()
        form.addRow("Model", self.model)
        self.effort = QComboBox()
        self.effort.addItem("Default", "")
        for effort in ("low", "medium", "high", "xhigh", "max"):
            self.effort.addItem(effort, effort)
        form.addRow("Effort", self.effort)
        self.recurrence = QComboBox()
        self.recurrence.addItem("One shot", "ONE_SHOT")
        self.recurrence.addItem("Every reset", "EVERY_RESET")
        form.addRow("Recurrence", self.recurrence)
        self.safety = QSpinBox()
        self.safety.setRange(0, 3600)
        self.safety.setValue(60)
        form.addRow("Reset safety (seconds)", self.safety)
        self.catchup = QSpinBox()
        self.catchup.setRange(0, 86400)
        self.catchup.setValue(900)
        form.addRow("Catch-up (seconds)", self.catchup)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        actions = QHBoxLayout()
        self.dry_button = QPushButton("Dry run")
        self.arm_button = QPushButton("Arm")
        self.close_button = QPushButton("Close")
        actions.addWidget(self.dry_button)
        actions.addWidget(self.arm_button)
        actions.addWidget(self.close_button)
        root.addLayout(actions)
        self.trigger.currentIndexChanged.connect(self._dynamic)
        self.payload.currentIndexChanged.connect(self._dynamic)
        self.account.currentIndexChanged.connect(self._dynamic)
        self.dry_button.clicked.connect(self.dry_run)
        self.arm_button.clicked.connect(self.arm)
        self.close_button.clicked.connect(self.reject)
        if job:
            self._load(job)
        self._dynamic()

    def _add_sync_member_row(self, account_id: str = "", launcher_id: str = "", model: str = "", effort: str = "") -> None:
        row = self.sync_table.rowCount()
        self.sync_table.insertRow(row)
        acc_combo = QComboBox()
        for account in self.accounts:
            acc_combo.addItem(account.display_name, account.account_id)
        if account_id:
            idx = acc_combo.findData(account_id)
            if idx >= 0:
                acc_combo.setCurrentIndex(idx)
        launcher_edit = QLineEdit(launcher_id)
        model_edit = QLineEdit(model)
        effort_combo = QComboBox()
        effort_combo.addItem("Default", "")
        for eff in ("low", "medium", "high", "xhigh", "max"):
            effort_combo.addItem(eff, eff)
        if effort:
            idx = effort_combo.findData(effort)
            if idx >= 0:
                effort_combo.setCurrentIndex(idx)

        def on_acc_changed():
            sel_id = acc_combo.currentData()
            acc = next((a for a in self.accounts if a.account_id == sel_id), None)
            if acc and acc.launcher_ids:
                launcher_edit.setText(acc.launcher_ids[0])
            self._dynamic()

        acc_combo.currentIndexChanged.connect(on_acc_changed)
        if not launcher_id:
            on_acc_changed()
        self.sync_table.setCellWidget(row, 0, acc_combo)
        self.sync_table.setCellWidget(row, 1, launcher_edit)
        self.sync_table.setCellWidget(row, 2, model_edit)
        self.sync_table.setCellWidget(row, 3, effort_combo)
        self._dynamic()

    def _remove_sync_member_row(self) -> None:
        row = self.sync_table.currentRow()
        if row >= 0:
            self.sync_table.removeRow(row)
        elif self.sync_table.rowCount() > 0:
            self.sync_table.removeRow(self.sync_table.rowCount() - 1)
        self._dynamic()

    def _get_sync_members_from_table(self, prepared_id: str) -> list[PreparedSyncMember]:
        members = []
        for row in range(self.sync_table.rowCount()):
            acc_widget = self.sync_table.cellWidget(row, 0)
            launch_widget = self.sync_table.cellWidget(row, 1)
            model_widget = self.sync_table.cellWidget(row, 2)
            effort_widget = self.sync_table.cellWidget(row, 3)
            acc_id = acc_widget.currentData() if isinstance(acc_widget, QComboBox) else ""
            launch_id = launch_widget.text().strip() if isinstance(launch_widget, QLineEdit) else ""
            model = model_widget.text().strip() if isinstance(model_widget, QLineEdit) else ""
            effort = effort_widget.currentData() if isinstance(effort_widget, QComboBox) else ""
            if acc_id:
                members.append(PreparedSyncMember(
                    prepared_id=prepared_id,
                    member_index=row,
                    account_id=acc_id,
                    launcher_id=launch_id,
                    model=model,
                    effort=effort,
                ))
        return members

    def _load(self, job: PreparedJob) -> None:
        for combo, value in ((self.project, job.project_id), (self.account, job.account_id),
                             (self.trigger, job.trigger.value), (self.payload, job.payload.value),
                             (self.recurrence, job.recurrence), (self.effort, job.effort),
                             (self.window, job.trigger_config.get("window_id")),
                             (self.policy, job.trigger_config.get("availability_policy"))):
            idx = combo.findData(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        if job.trigger_config.get("at"):
            self.at.setDateTime(QDateTime.fromSecsSinceEpoch(int(parse_time(job.trigger_config["at"]).timestamp())))
        self.text.setPlainText(str(job.payload_config.get("text") or ""))
        self.path.setText(str(job.payload_config.get("path") or ""))
        self.audit_profile.setText(str(job.payload_config.get("profile_id") or ""))
        self.model.setText(job.model)
        provider_index = self.auto_provider.findData(job.trigger_config.get("auto_provider_id", ""))
        if provider_index >= 0:
            self.auto_provider.setCurrentIndex(provider_index)
        self.safety.setValue(job.safety_delay_seconds)
        self.catchup.setValue(job.catch_up_seconds)
        if job.trigger == Trigger.SYNC:
            members = PreparedStore().get_sync_members(job.prepared_id)
            self.sync_table.setRowCount(0)
            for m in members:
                self._add_sync_member_row(m.account_id, m.launcher_id, m.model, m.effort)

    def _dynamic(self) -> None:
        trigger = self.trigger.currentData()
        payload = self.payload.currentData()
        def show(field, visible):
            field.setVisible(visible)
            label = self.form.labelForField(field)
            if label is not None:
                label.setVisible(visible)
        is_sync = trigger == Trigger.SYNC.value
        show(self.account, not is_sync)
        show(self.sync_members_widget, is_sync)
        show(self.at, trigger in (Trigger.ON_TIME.value, Trigger.SYNC.value, Trigger.PRIME.value))
        show(self.window, trigger == Trigger.ON_RESET.value)
        show(self.policy, trigger == Trigger.ON_TIME.value)
        show(self.safety, trigger == Trigger.ON_RESET.value)
        show(self.recurrence, trigger == Trigger.ON_RESET.value)
        show(self.text, payload in (Payload.USER_COMMAND.value, Payload.STATIC_PROMPT.value))
        show(self.path, payload == Payload.PINNED_SAIHANDOFF.value)
        show(self.audit_profile, payload == Payload.AUDIT.value)
        show(self.auto_provider, self.account.currentData() == "AUTO" and not is_sync)
        account = next((a for a in self.accounts if a.account_id == self.account.currentData()), None)
        auto = self.account.currentData() == "AUTO" and not is_sync
        self.effort.setEnabled((account is not None and account.provider_id in ("codex", "claude")) or auto)
        is_audit = payload == Payload.AUDIT.value
        time_based = trigger in (Trigger.ON_TIME.value, Trigger.ON_RESET.value)
        if is_audit:
            # AUDIT is armable when the trigger is time-based and the account is
            # explicit. Runtime capability gates (account binding, model/effort,
            # profile, freshness) are enforced by the Bridge worker with exact
            # wait reasons, so the editor must not fake-block a valid audit.
            supported = time_based and not auto
        elif is_sync:
            supported = False
        else:
            supported = (trigger == Trigger.ON_TIME.value if auto else time_based)
        self.arm_button.setEnabled(supported)
        if not supported:
            if is_sync:
                members = self._get_sync_members_from_table(self.job.prepared_id if self.job else "draft")
                if len(members) >= 2:
                    acc_ids = [m.account_id for m in members]
                    if len(acc_ids) != len(set(acc_ids)):
                        self.message.setText("SYNC requires distinct accounts.")
                    else:
                        m_acc = next((a for a in self.accounts if a.account_id == members[0].account_id), None)
                        self.message.setText(workflow_gate_reason(
                            Trigger(trigger), Payload(payload), m_acc, auto=False))
                else:
                    self.message.setText(workflow_gate_reason(
                        Trigger(trigger), Payload(payload), account, auto))
            else:
                self.message.setText(workflow_gate_reason(
                    Trigger(trigger), Payload(payload), account, auto))

    def build_job(self, *, enabled: bool) -> PreparedJob:
        auto = self.account.currentData() == "AUTO"
        account = next((a for a in self.accounts if a.account_id == self.account.currentData()), None)
        trigger = Trigger(self.trigger.currentData())
        if trigger == Trigger.SYNC:
            members = self._get_sync_members_from_table(self.job.prepared_id if self.job else "draft")
            if not auto and account is None and members:
                account = next((a for a in self.accounts if a.account_id == members[0].account_id), None)
        if not auto and trigger != Trigger.SYNC and (account is None or len(account.launcher_ids) != 1):
            raise PreflightError("select one bound account")
        if auto and trigger != Trigger.ON_TIME:
            raise PreflightError("AUTO currently supports On time only")
        payload = Payload(self.payload.currentData())
        config = ({"window_id": self.window.currentData()} if trigger == Trigger.ON_RESET else {
            "at": datetime.fromtimestamp(self.at.dateTime().toSecsSinceEpoch(), timezone.utc).isoformat(),
            "availability_policy": self.policy.currentData(),
        })
        if auto and self.auto_provider.currentData():
            config["auto_provider_id"] = self.auto_provider.currentData()
        next_due = config["at"] if trigger in (Trigger.ON_TIME, Trigger.SYNC, Trigger.PRIME) else ""
        payload_config = ({"text": self.text.toPlainText()} if payload in (Payload.USER_COMMAND, Payload.STATIC_PROMPT)
                          else {"path": self.path.text()} if payload == Payload.PINNED_SAIHANDOFF
                          else {"profile_id": self.audit_profile.text()} if payload == Payload.AUDIT else {})
        now = datetime.now(timezone.utc).isoformat()
        prepared_id = self.job.prepared_id if self.job else uuid.uuid4().hex
        return PreparedJob(
            prepared_id=prepared_id,
            name=self.name.text().strip() or "Prepared launch",
            project_id=str(self.project.currentData() or ""),
            launcher_id="AUTO" if auto else (account.launcher_ids[0] if account and account.launcher_ids else "SYNC"),
            account_id="AUTO" if auto else (account.account_id if account else "SYNC"),
            trigger=trigger, payload=payload, payload_config=payload_config,
            trigger_config=config, model=self.model.text().strip(), effort=str(self.effort.currentData() or ""),
            enabled=enabled, state=JobState.ARMED if enabled else JobState.DRAFT,
            recurrence=self.recurrence.currentData() if trigger == Trigger.ON_RESET else "ONE_SHOT",
            safety_delay_seconds=self.safety.value(), catch_up_seconds=self.catchup.value(),
            next_due_at=next_due,
            created_at=self.job.created_at if self.job else now, updated_at=now,
        )

    def dry_run(self) -> None:
        try:
            job = self.build_job(enabled=False)
            if job.trigger == Trigger.SYNC:
                members = self._get_sync_members_from_table(job.prepared_id)
                member_desc = ", ".join(f"{m.account_id}[{m.launcher_id}]" for m in members) or "none configured"
                self.message.setText(
                    f"READY (SYNC configuration draft)\nMembers ({len(members)}): {member_desc}\n"
                    f"Project: {job.project_id}\nTrigger: SYNC\n"
                    f"Scheduled: {job.next_due_at or 'not set'}"
                )
                self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                return
            account = self._preview_account(job)
            if job.payload == Payload.PINNED_SAIHANDOFF and not job.payload_config.get("sha256"):
                _, inspected = inspect_handoff(Path(job.payload_config["path"]),
                                               job.project_id, self.config.projects)
                job = replace(job, payload_config={"path": str(inspected.path),
                                                   "sha256": inspected.sha256})
            resolved = replace(job, account_id=account.account_id,
                               launcher_id=account.launcher_ids[0])
            plan = build_launch_plan(resolved, account, self.config)
            status = LimitStore().get(account.account_id)
            availability = status[0].availability().value if status else Availability.UNKNOWN.value
            readiness = "READY" if availability in (Availability.AVAILABLE.value, Availability.LOW.value) else "WAITING_LIMIT"
            self.message.setText(
                f"{readiness}\nAccount: {account.display_name}\nProject: {job.project_id}\n"
                f"Trigger: {job.trigger.value}\nPayload: {plan.payload_preview}\n"
                f"Model: {job.model or 'default'}  Effort: {job.effort or 'default'}\n"
                f"Limit: {availability}\nDelivery: {plan.delivery_mode}\n"
                f"SHA-256: {plan.payload_sha256}"
            )
            self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        except Exception as exc:
            self.message.setText(f"BLOCKED: {exc}")

    def arm(self) -> None:
        try:
            job = self.build_job(enabled=True)
            if job.trigger not in (Trigger.ON_TIME, Trigger.ON_RESET) or job.payload == Payload.AUDIT:
                raise PreflightError("workflow not verified")
            account = self._preview_account(job)
            if job.payload == Payload.PINNED_SAIHANDOFF:
                pinned = pin_handoff(Path(job.payload_config["path"]), job.project_id, self.config.projects)
                job = replace(job, payload_config={"path": str(pinned.path), "sha256": pinned.sha256})
            resolved = replace(job, account_id=account.account_id,
                               launcher_id=account.launcher_ids[0])
            build_launch_plan(resolved, account, self.config)
            PreparedStore().save(job)
            if job.trigger == Trigger.SYNC:
                members = self._get_sync_members_from_table(job.prepared_id)
                PreparedStore().save_sync_members(job.prepared_id, members)
            self.accept()
        except Exception as exc:
            self.message.setText(f"BLOCKED: {exc}")

    def _preview_account(self, job: PreparedJob) -> AccountIdentity:
        if job.account_id != "AUTO":
            return next(a for a in self.accounts if a.account_id == job.account_id)
        provider = str(job.trigger_config.get("auto_provider_id") or "")
        candidates = [account for account in self.accounts
                      if account.enabled and len(account.launcher_ids) == 1 and
                      (not provider or account.provider_id == provider)]
        if not candidates:
            raise PreflightError("AUTO has no bound account for this provider")
        return sorted(candidates, key=lambda account: account.account_id)[0]


class LimitsPreparedWidget(QWidget):
    snapshots_changed = Signal(dict)

    def __init__(self, config, task_runner, parent=None):
        super().__init__(parent)
        self.config = config
        self.runner = task_runner
        self.accounts: list[AccountIdentity] = []
        self.jobs: list[PreparedJob] = []
        layout = QVBoxLayout(self)
        self.summary = QLabel("Accounts: loading  •  Prepared: loading")
        layout.addWidget(self.summary)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter provider, account, launcher or availability")
        self.filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter)
        self.account_table = QTableWidget(0, 10)
        self.account_table.setHorizontalHeaderLabels(
            ["Provider", "Account", "Launcher", "5h", "Weekly", "Other", "Available", "Reset", "Last check", "Next job"])
        self.account_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.account_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.account_table.setSortingEnabled(True)
        layout.addWidget(self.account_table)
        self.job_table = QTableWidget(0, 6)
        self.job_table.setHorizontalHeaderLabels(["Name", "Project", "Account", "Trigger", "State", "Next job"])
        self.job_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.job_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.job_table.setSortingEnabled(True)
        layout.addWidget(self.job_table)
        buttons = QHBoxLayout()
        for label, handler in (("Refresh", self.refresh_selected), ("Bind", self.bind_selected),
                               ("Prepare", self.prepare),
                               ("Edit", self.edit_selected), ("Disarm", self.disarm_selected),
                               ("Test now", self.test_now_selected)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        recovery_button = QPushButton("Resolve interrupted test")
        recovery_button.clicked.connect(self.resolve_test_selected)
        layout.addWidget(recovery_button)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.setInterval(60000)
        self.timer.timeout.connect(self.refresh_view)
        self.timer.start()
        self.refresh_view()

    def refresh_view(self) -> None:
        def load():
            registry = AccountRegistry()
            discovered = discover_accounts(self.config.launchers)
            registry.upsert(discovered)
            present = {account.account_id for account in discovered}
            accounts = registry.list()
            limits = LimitStore()
            snapshots = {a.account_id: limits.get(a.account_id) for a in accounts}
            store = PreparedStore()
            jobs = store.list()
            recoveries = {job.prepared_id: receipt for job in jobs
                          if (receipt := store.test_recovery(job.prepared_id)) is not None}
            return accounts, snapshots, jobs, recoveries, present

        def apply(result):
            self.accounts, snapshots, self.jobs, recoveries, present = result
            account_by_id = {a.account_id: a for a in self.accounts}
            self.account_table.setSortingEnabled(False)
            self.job_table.setSortingEnabled(False)
            self.account_table.setRowCount(len(self.accounts))
            snapshot_by_launcher = {}
            for row, account in enumerate(self.accounts):
                stored = snapshots.get(account.account_id)
                snapshot = stored[0] if stored else None
                discovered = account.account_id in present
                windows = snapshot.windows if snapshot else ()
                reset = min((parse_time(w.reset_at) for w in windows if w.reset_at), default=None)
                next_job = next((j for j in self.jobs if j.account_id == account.account_id and j.enabled), None)
                # Origin is visible where it changes what the row MEANS: a shared
                # account is read through the SAI Accounts plane rather than this
                # machine's own CLI, and it starts UNBOUND by design.
                shared = account.discovery_source == sai_accounts.SHARED_SOURCE
                name = account.display_name
                if shared:
                    name = f"{name} (shared)"
                values = [account.provider_id, name if discovered
                          else f"{name} (missing)",
                          ", ".join(account.launcher_ids) or "UNBOUND", _meter(windows, "five_hour"),
                          _meter(windows, "weekly"), str(max(0, len(windows) - 2)) if windows else "—",
                          snapshot.availability().value if discovered and snapshot else
                          "MISSING" if not discovered else Availability.UNKNOWN.value,
                          _display_time(reset.isoformat() if reset else None),
                          _display_time(snapshot.observed_at if snapshot else None),
                          _display_time(next_job.next_due_at) if next_job else "—"]
                tooltip = limit_tooltip(account, snapshot, stored[1] if stored else None, next_job)
                if shared:
                    tooltip = ("Shared account from SAI Accounts. Its usage is read through "
                               "the control plane, which owns that identity.\n" + tooltip)
                if not discovered:
                    tooltip = "Account not discovered on this refresh.\n" + tooltip
                for col, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    cell.setToolTip(tooltip)
                    cell.setData(Qt.ItemDataRole.UserRole, account.account_id)
                    self.account_table.setItem(row, col, cell)
                if discovered:
                    for launcher in account.launcher_ids:
                        snapshot_by_launcher[launcher] = (account, snapshot, tooltip)
            self.job_table.setRowCount(len(self.jobs))
            for row, job in enumerate(self.jobs):
                recovery = recoveries.get(job.prepared_id)
                if job.trigger == Trigger.SYNC:
                    account_label = "SYNC"
                elif job.account_id == "AUTO":
                    account_label = "AUTO"
                else:
                    account = account_by_id.get(job.account_id)
                    account_label = (account.display_name if account else "Unknown")
                    if account and account.account_id not in present:
                        account_label += " (missing)"
                state_label = ("TEST RECOVERY" if recovery else job.state.value)
                if not recovery and job.waiting_reason:
                    state_label += f": {job.waiting_reason}"
                values = [job.name, job.project_id, account_label,
                          job.trigger.value, state_label,
                          _display_time(job.next_due_at)]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip("Interrupted test outcome uncertain; inspect its process before resolving."
                                    if recovery else job.waiting_reason)
                    item.setData(Qt.ItemDataRole.UserRole, job.prepared_id)
                    self.job_table.setItem(row, col, item)
            self.account_table.setSortingEnabled(True)
            self.job_table.setSortingEnabled(True)
            self._apply_filter()
            self.summary.setText(f"Accounts: {len(self.accounts)}  •  Prepared: "
                                 f"{sum(j.enabled for j in self.jobs)} armed  •  "
                                 f"Account missing: {sum(j.enabled and j.waiting_reason == 'account not discovered' for j in self.jobs)}  •  "
                                 f"Test recovery: {len(recoveries)}")
            self.snapshots_changed.emit(snapshot_by_launcher)

        self.runner.submit_coalesced("prepared:view", load, on_success=apply)

    def _apply_filter(self) -> None:
        needle = self.filter.text().strip().casefold()
        for table in (self.account_table, self.job_table):
            for row in range(table.rowCount()):
                haystack = " ".join(table.item(row, col).text() for col in range(table.columnCount())
                                    if table.item(row, col) is not None).casefold()
                table.setRowHidden(row, bool(needle and needle not in haystack))

    def _selected_account(self) -> AccountIdentity | None:
        row = self.account_table.currentRow()
        item = self.account_table.item(row, 0) if row >= 0 else None
        identity = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((account for account in self.accounts if account.account_id == identity), None)

    def _selected_job(self) -> PreparedJob | None:
        row = self.job_table.currentRow()
        item = self.job_table.item(row, 0) if row >= 0 else None
        identity = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((job for job in self.jobs if job.prepared_id == identity), None)

    def refresh_selected(self) -> None:
        account = self._selected_account()
        if account is None:
            self.status.setText("Select an account to refresh.")
            return
        def probe():
            coordinator = LimitCoordinator({"codex": CodexLimitAdapter(), "claude": ClaudeLimitAdapter(),
                                            "antigravity": AntigravityLimitAdapter()}, LimitStore())
            return coordinator.refresh(account, force=True)
        self.status.setText(f"Refreshing {account.display_name}...")
        self.runner.submit_coalesced(f"prepared:probe:{account.account_id}", probe,
                                     on_success=lambda _snapshot: (self.status.setText("Refresh finished."), self.refresh_view()),
                                     on_error=lambda error: self.status.setText(f"Refresh failed: {type(error).__name__}"))

    def bind_selected(self) -> None:
        account = self._selected_account()
        if account is None or account.bound:
            self.status.setText("Select an UNBOUND account.")
            return
        used = {launcher for other in self.accounts if other.account_id != account.account_id
                for launcher in other.launcher_ids}
        choices = [launcher for launcher in self.config.launchers if launcher.enabled
                   and launcher.id not in used
                   and account.provider_id in f"{launcher.id} {launcher.name}".casefold()]
        if not choices:
            self.status.setText("No compatible free launcher is configured.")
            return
        labels = [f"{launcher.name} [{launcher.id}]" for launcher in choices]
        selected, accepted = QInputDialog.getItem(
            self, "Bind account", f"Launcher for {account.display_name}", labels, 0, False,
        )
        if not accepted:
            return
        chosen = choices[labels.index(selected)]
        try:
            AccountRegistry().bind(account.account_id, chosen.id)
        except ValueError as exc:
            self.status.setText(f"Binding failed: {exc}")
            return
        self.status.setText(f"Bound {account.display_name} to {chosen.name}.")
        self.refresh_view()

    def prepare(self) -> None:
        editor = PreparedEditor(self.config, self.accounts, self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            self.status.setText("Prepared job armed.")
            self.refresh_view()

    def edit_selected(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        editor = PreparedEditor(self.config, self.accounts, self, job)
        if editor.exec() == QDialog.DialogCode.Accepted:
            self.status.setText("Prepared job updated.")
            self.refresh_view()

    def disarm_selected(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        try:
            PreparedStore().save(replace(job, enabled=False, state=JobState.DISABLED))
            self.status.setText(f"Disarmed {job.name}.")
            self.refresh_view()
        except Exception as exc:
            self.status.setText(f"Cannot disarm: {exc}")

    def resolve_test_selected(self) -> None:
        job = self._selected_job()
        if job is None:
            self.status.setText("Select a prepared job with TEST RECOVERY.")
            return
        store = PreparedStore()
        receipt = store.test_recovery(job.prepared_id)
        if receipt is None:
            self.status.setText("No interrupted test needs recovery for this job.")
            return
        pid = receipt["process_id"]
        detail = f"PID {pid}" if pid else "no recorded PID"
        answer = QMessageBox.question(
            self, "Resolve interrupted test",
            f"Inspect the interrupted test ({detail}) first.\n"
            "Confirm its process is no longer working and release this scheduled job?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if store.resolve_test_recovery(receipt["execution_id"],
                                       "operator confirmed interrupted test ended"):
            self.status.setText(f"Test recovery resolved for {job.name}; schedule remains armed.")
            self.refresh_view()
        else:
            self.status.setText("Test recovery changed; refresh and inspect again.")

    def test_now_selected(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        account = next((a for a in self.accounts if a.account_id == job.account_id), None)
        if account is None:
            self.status.setText("Account unavailable.")
            return
        stored = LimitStore().get(account.account_id)
        if not stored or stored[0].availability() not in (Availability.AVAILABLE, Availability.LOW):
            self.status.setText("Limit state does not prove account available. Refresh first.")
            return
        def run():
            store = PreparedStore()
            plan = build_launch_plan(job, account, self.config)
            owner = uuid.uuid4().hex
            claimed = store.claim_test(job.prepared_id, owner)
            if claimed is None:
                raise RuntimeError("job disarmed")
            execution, event = claimed
            receipt = store.receipt(job.prepared_id, event)
            return execute_claimed(plan, job, store, execution, owner,
                                   receipt["claim_generation"])
        self.status.setText(f"Testing {job.name} now...")
        self.runner.submit_coalesced(f"prepared:test:{job.prepared_id}", run,
                                     on_success=lambda result: (self.status.setText(f"Test now: {result.state.value}"), self.refresh_view()),
                                     on_error=lambda error: self.status.setText(f"Test now failed: {type(error).__name__}"))
