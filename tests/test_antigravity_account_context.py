"""PHASE 0 of SRC-109: one execution context per Antigravity account.

The installed CLI has no process-local account isolation, so a context other
than this Windows sign-in must fail loudly instead of reporting this sign-in's
quota under another account's name.
"""

import pytest

from audapack.account_registry import AccountIdentity
from audapack.limit_adapters import (
    LOCAL_ANTIGRAVITY_CONTEXT,
    AntigravityAccountContext,
    AntigravityLimitAdapter,
    probe_env,
)

NOW = "2026-10-03T09:00:00+00:00"


def _account() -> AccountIdentity:
    return AccountIdentity("antigravity:local", "antigravity", "Antigravity",
                           "vac34", (), "local", NOW)


def test_default_context_is_the_legacy_single_account():
    adapter = AntigravityLimitAdapter(executable="agy.exe")
    assert adapter.context == LOCAL_ANTIGRAVITY_CONTEXT
    assert adapter.context.backend == "default"
    assert adapter.context.context_locator == ""


def test_probe_env_never_carries_a_credential(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    env = probe_env(LOCAL_ANTIGRAVITY_CONTEXT)
    assert "GEMINI_API_KEY" not in env
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert "ANTIGRAVITY_EXECUTABLE_DATA_DIR" not in env


def test_probe_env_applies_a_data_dir_override():
    context = AntigravityAccountContext("antigravity:b", "B", "windows_user",
                                        "seconduser", r"C:\Users\seconduser\AppData\Local\agy")
    assert probe_env(context)["ANTIGRAVITY_EXECUTABLE_DATA_DIR"] == context.data_dir


@pytest.mark.parametrize("backend,locator", [("windows_user", "seconduser"),
                                             ("gemini_api_key", "")])
def test_foreign_context_refuses_to_probe_locally(backend, locator):
    context = AntigravityAccountContext(f"antigravity:{backend}", "Second", backend, locator)
    adapter = AntigravityLimitAdapter(executable="agy.exe", context=context)
    with pytest.raises(RuntimeError, match=f"antigravity_context_unavailable:antigravity:{backend}"):
        adapter.probe_limits(_account())
