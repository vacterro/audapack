from datetime import datetime, timezone

from audapack.account_registry import AccountIdentity, AccountRegistry
from audapack.config import AppConfig, LauncherConfig
from audapack.limits import LimitStore
from audapack.models import Project
from audapack.prepared import JobState, Payload, PreparedJob, PreparedStore, Trigger
from audapack.ui_qt.dialogs.limits_prepared_widget import (
    LimitsPreparedWidget,
    PreparedEditor,
    limit_tooltip,
    workflow_gate_reason,
)


def test_editor_dynamic_fields_dry_run_and_arm(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr("audapack.prepared_delivery.shutil.which", lambda _name: "codex.cmd")
    monkeypatch.setattr("audapack.prepared_delivery._verify_local_cli_contract", lambda *_args: None)
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", datetime.now(timezone.utc).isoformat())
    editor = PreparedEditor(config, [account])
    editor.trigger.setCurrentIndex(editor.trigger.findData(Trigger.ON_TIME.value))
    editor.payload.setCurrentIndex(editor.payload.findData(Payload.USER_COMMAND.value))
    editor.text.setPlainText("cc")
    editor.name.setText("Continue")
    editor.show()
    qapp.processEvents()
    assert editor.at.isVisible()
    assert editor.window.isHidden()
    assert editor.form.labelForField(editor.window).isHidden()
    editor.dry_run()
    assert "WAITING_LIMIT" in editor.message.text()
    assert "SHA-256" in editor.message.text()
    editor.arm()
    jobs = PreparedStore().list()
    assert len(jobs) == 1
    assert jobs[0].payload_config == {"text": "cc"}
    assert jobs[0].enabled
    editor.close()


def test_tooltip_shows_unknown_without_fake_percentage(tmp_path):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", datetime.now(timezone.utc).isoformat())
    text = limit_tooltip(account, None)
    assert "UNKNOWN" in text
    assert "%" not in text


def test_pinned_handoff_dry_run_validates_without_creating_copy(tmp_path, qapp, monkeypatch):
    monkeypatch.setattr("audapack.prepared_delivery.shutil.which", lambda _name: "codex.cmd")
    monkeypatch.setattr("audapack.prepared_delivery._verify_local_cli_contract", lambda *_args: None)
    monkeypatch.setattr("audapack.prepared_payloads.get_state_dir", lambda: tmp_path)
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", datetime.now(timezone.utc).isoformat())
    source = tmp_path / "selected.md"
    source.write_text("Project — SAIHANDOFF — prepared\nExact payload\n", encoding="utf-8")
    editor = PreparedEditor(config, [account])
    editor.trigger.setCurrentIndex(editor.trigger.findData(Trigger.ON_TIME.value))
    editor.payload.setCurrentIndex(editor.payload.findData(Payload.PINNED_SAIHANDOFF.value))
    editor.path.setText(str(source))
    editor.dry_run()
    assert "SHA-256" in editor.message.text()
    assert not (tmp_path / "prepared_handoffs").exists()
    editor.close()


def test_missing_account_and_wait_reason_are_visible_in_limits_view(tmp_path, qapp, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", datetime.now(timezone.utc).isoformat())
    config = AppConfig()
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    registry = AccountRegistry(path)
    registry.upsert([account])
    jobs = PreparedStore(path)
    jobs.save(PreparedJob("job", "continue", "project", "main_codex2",
                          account.account_id, Trigger.ON_TIME, Payload.USER_COMMAND,
                          {"text": "cc"}, {"at": "2030-01-01T00:00:00+00:00"},
                          enabled=True, state=JobState.WAITING_LIMIT,
                          waiting_reason="account not discovered"))
    limits = LimitStore(path)
    monkeypatch.setattr("audapack.ui_qt.dialogs.limits_prepared_widget.AccountRegistry",
                        lambda: registry)
    monkeypatch.setattr("audapack.ui_qt.dialogs.limits_prepared_widget.PreparedStore",
                        lambda: jobs)
    monkeypatch.setattr("audapack.ui_qt.dialogs.limits_prepared_widget.LimitStore",
                        lambda: limits)
    monkeypatch.setattr("audapack.ui_qt.dialogs.limits_prepared_widget.discover_accounts",
                        lambda _launchers: [])

    class InlineRunner:
        def submit_coalesced(self, _key, fn, on_success, on_error=None):
            on_success(fn())

    widget = LimitsPreparedWidget(config, InlineRunner())
    assert widget.account_table.item(0, 1).text() == "Codex 2 (missing)"
    assert widget.account_table.item(0, 6).text() == "MISSING"
    assert "account not discovered" in widget.job_table.item(0, 4).text()
    assert "Account missing: 1" in widget.summary.text()
    widget.timer.stop()
    widget.close()


def test_edit_preserves_time_policy_and_reset_window_and_recomputes_due(tmp_path, qapp):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", datetime.now(timezone.utc).isoformat())
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    old_due = "2030-01-01T10:00:00+00:00"
    on_time = PreparedJob("job", "strict", "project", "main_codex2", account.account_id,
                          Trigger.ON_TIME, Payload.USER_COMMAND, {"text": "cc"},
                          {"at": old_due, "availability_policy": "STRICT_TIME"},
                          enabled=True, state=JobState.ARMED, next_due_at=old_due)
    editor = PreparedEditor(config, [account], job=on_time)
    assert editor.policy.currentData() == "STRICT_TIME"
    editor.at.setDateTime(editor.at.dateTime().addSecs(3600))
    updated = editor.build_job(enabled=True)
    assert updated.trigger_config["availability_policy"] == "STRICT_TIME"
    assert updated.next_due_at == updated.trigger_config["at"]
    assert updated.next_due_at != old_due
    editor.close()

    on_reset = PreparedJob("reset", "weekly", "project", "main_codex2",
                           account.account_id, Trigger.ON_RESET, Payload.USER_COMMAND,
                           {"text": "cc"}, {"window_id": "weekly"},
                           enabled=True, state=JobState.ARMED)
    editor = PreparedEditor(config, [account], job=on_reset)
    assert editor.window.currentData() == "weekly"
    updated = editor.build_job(enabled=True)
    assert updated.trigger_config["window_id"] == "weekly"
    assert updated.next_due_at == ""
    editor.close()


def test_sync_prime_audit_arm_gate_names_exact_reason(tmp_path, qapp):
    from dataclasses import replace as _replace

    from audapack.provider_capabilities import PROVIDERS, WindowStartSemantics

    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", datetime.now(timezone.utc).isoformat())
    # SYNC on a provider whose window semantics are UNKNOWN: honest reason,
    # never a bare "SYNC disabled".
    reason = workflow_gate_reason(Trigger.SYNC, Payload.USER_COMMAND, account, auto=False)
    assert "SYNC unavailable" in reason and "UNKNOWN" in reason
    # AUDIT with a time-based trigger and an explicit account is now armable:
    # the Bridge worker enforces the runtime capability gates (account binding,
    # model/effort, profile, freshness) with exact wait reasons, so the editor
    # no longer fake-blocks a valid audit.
    audit_reason = workflow_gate_reason(Trigger.ON_TIME, Payload.AUDIT, account, auto=False)
    assert audit_reason == ""
    # AUDIT under AUTO is refused explicitly (needs a bound account for binding).
    audit_auto = workflow_gate_reason(Trigger.ON_TIME, Payload.AUDIT, account, auto=True)
    assert "AUTO" in audit_auto
    # AUTO + advanced trigger is refused explicitly.
    auto_reason = workflow_gate_reason(Trigger.PRIME, Payload.USER_COMMAND, account, auto=True)
    assert "AUTO" in auto_reason
    # A proven-capable provider surfaces the runtime gap, not fake semantics.
    caps = _replace(PROVIDERS["codex"], supports_sync=True,
                    window_start_semantics=WindowStartSemantics.FIRST_CONSUMING_USE)
    original = PROVIDERS["codex"]
    PROVIDERS["codex"] = caps
    try:
        runtime_reason = workflow_gate_reason(Trigger.SYNC, Payload.USER_COMMAND, account, auto=False)
    finally:
        PROVIDERS["codex"] = original
    assert "worker runtime not connected" in runtime_reason


def test_editor_sync_trigger_disables_arm_with_visible_reason(tmp_path, qapp):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", datetime.now(timezone.utc).isoformat())
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    editor = PreparedEditor(config, [account])
    editor.account.setCurrentIndex(editor.account.findData(account.account_id))
    editor.trigger.setCurrentIndex(editor.trigger.findData(Trigger.SYNC.value))
    editor.payload.setCurrentIndex(editor.payload.findData(Payload.USER_COMMAND.value))
    assert not editor.arm_button.isEnabled()
    assert "SYNC unavailable" in editor.message.text()
    editor.close()


def test_editor_prime_trigger_disables_arm_with_visible_reason(tmp_path, qapp):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", datetime.now(timezone.utc).isoformat())
    config = AppConfig()
    config.projects = [Project("project", "Project", str(tmp_path))]
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    editor = PreparedEditor(config, [account])
    editor.account.setCurrentIndex(editor.account.findData(account.account_id))
    editor.trigger.setCurrentIndex(editor.trigger.findData(Trigger.PRIME.value))
    editor.payload.setCurrentIndex(editor.payload.findData(Payload.USER_COMMAND.value))
    assert not editor.arm_button.isEnabled()
    assert "PRIME unavailable" in editor.message.text()
    editor.close()


def test_job_table_displays_auto_and_sync_labels(tmp_path, qapp, monkeypatch):
    account = AccountIdentity("codex:2", "codex", "Codex 2", str(tmp_path),
                              ("main_codex2",), "test", datetime.now(timezone.utc).isoformat())
    config = AppConfig()
    config.launchers = [LauncherConfig("main_codex2", "Codex 2", "C2")]
    path = tmp_path / "resources.sqlite3"
    registry = AccountRegistry(path)
    registry.upsert([account])
    jobs = PreparedStore(path)
    jobs.save(PreparedJob("auto-job", "Auto Job", "proj", "AUTO", "AUTO", Trigger.ON_TIME,
                          Payload.USER_COMMAND, {"text": "hi"}, {"at": "2030-01-01T00:00:00+00:00"},
                          enabled=True, state=JobState.ARMED))
    jobs.save(PreparedJob("sync-job", "Sync Job", "proj", "SYNC", "SYNC", Trigger.SYNC,
                          Payload.USER_COMMAND, {"text": "hi"}, {"at": "2030-01-01T00:00:00+00:00"},
                          enabled=True, state=JobState.ARMED))
    limits = LimitStore(path)
    monkeypatch.setattr("audapack.ui_qt.dialogs.limits_prepared_widget.AccountRegistry", lambda: registry)
    monkeypatch.setattr("audapack.ui_qt.dialogs.limits_prepared_widget.PreparedStore", lambda: jobs)
    monkeypatch.setattr("audapack.ui_qt.dialogs.limits_prepared_widget.LimitStore", lambda: limits)
    monkeypatch.setattr("audapack.ui_qt.dialogs.limits_prepared_widget.discover_accounts", lambda _l: [account])

    class InlineRunner:
        def submit_coalesced(self, _key, fn, on_success, on_error=None):
            on_success(fn())

    widget = LimitsPreparedWidget(config, InlineRunner())
    account_labels = [widget.job_table.item(row, 2).text() for row in range(widget.job_table.rowCount())]
    assert "AUTO" in account_labels
    assert "SYNC" in account_labels
    widget.close()

