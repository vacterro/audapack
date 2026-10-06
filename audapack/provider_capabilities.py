"""Conservative local provider contracts for prepared execution and quota windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class WindowStartSemantics(str, Enum):
    FIRST_CONSUMING_USE = "FIRST_CONSUMING_USE"
    FIXED = "FIXED"
    ROLLING = "ROLLING"
    PROVIDER_ASSIGNED = "PROVIDER_ASSIGNED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CapabilityEvidence:
    source: str
    confidence: str
    observed_at: str = ""
    cli_version: str = ""


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_id: str
    supports_prepared_prompt: bool
    supports_model_selection: bool
    supports_effort_selection: bool
    supported_efforts: frozenset[str]
    supports_limit_probe: bool
    supports_prime: bool
    supports_sync: bool
    window_start_semantics: WindowStartSemantics
    quota_bucket_mapping: dict[str, str]
    evidence: CapabilityEvidence

    def bucket_for_model(self, model: str) -> str | None:
        return self.quota_bucket_mapping.get(model)


_UNKNOWN = WindowStartSemantics.UNKNOWN
_IMPLEMENTATION = CapabilityEvidence("local_provider_adapter", "IMPLEMENTED")

PROVIDERS: dict[str, ProviderCapabilities] = {
    "codex": ProviderCapabilities(
        "codex", True, True, True,
        frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
        True, False, False, _UNKNOWN, {}, _IMPLEMENTATION,
    ),
    "claude": ProviderCapabilities(
        "claude", True, True, True,
        frozenset({"low", "medium", "high", "xhigh", "max"}),
        True, False, False, _UNKNOWN, {}, _IMPLEMENTATION,
    ),
    "antigravity": ProviderCapabilities(
        "antigravity", False, False, False, frozenset(),
        True, False, False, _UNKNOWN, {}, _IMPLEMENTATION,
    ),
    "zcode": ProviderCapabilities(
        "zcode", False, False, False, frozenset(),
        False, False, False, _UNKNOWN, {}, _IMPLEMENTATION,
    ),
}


def with_local_cli_evidence(capabilities: ProviderCapabilities, version: str,
                            observed_at: datetime) -> ProviderCapabilities:
    """Annotate a verified CLI without promoting unknown window semantics."""
    from dataclasses import replace

    return replace(capabilities, evidence=CapabilityEvidence(
        "local_cli_help", "LOCAL_VERIFIED", observed_at.isoformat(), version,
    ))
