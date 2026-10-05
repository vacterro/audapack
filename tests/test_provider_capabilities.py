from datetime import datetime, timezone

from audapack.provider_capabilities import (
    PROVIDERS,
    WindowStartSemantics,
    with_local_cli_evidence,
)


def test_unverified_window_start_and_model_bucket_remain_unknown():
    for capabilities in PROVIDERS.values():
        assert capabilities.window_start_semantics is WindowStartSemantics.UNKNOWN
        assert not capabilities.supports_prime
        assert not capabilities.supports_sync
        assert capabilities.bucket_for_model("gpt-6-sol") is None


def test_cli_help_provenance_does_not_promote_quota_semantics():
    verified = with_local_cli_evidence(
        PROVIDERS["codex"], "codex-cli-test", datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    assert verified.evidence.source == "local_cli_help"
    assert verified.evidence.cli_version == "codex-cli-test"
    assert verified.window_start_semantics is WindowStartSemantics.UNKNOWN
    assert verified.bucket_for_model("gpt-6-sol") is None
