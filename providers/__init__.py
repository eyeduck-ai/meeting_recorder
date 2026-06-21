"""Meeting providers package.

This module provides a registry-based provider system for different meeting platforms.
New providers can be registered using `register_provider()`.
"""

from dataclasses import dataclass

from providers.base import BaseProvider, DiagnosticData, JoinResult, MeetingState, MeetingStateSnapshot
from providers.jitsi import JitsiProvider
from providers.webex import WebexProvider
from providers.zoom import ZoomProvider


@dataclass(frozen=True)
class ProviderMetadata:
    """UI/API metadata for a supported meeting provider."""

    name: str
    label: str
    meeting_code_label: str
    meeting_code_placeholder: str
    meeting_code_hint: str
    show_base_url: bool
    badge_class: str
    telegram_url_hint: str

    def form_config(self) -> dict:
        """Return the browser form config for this provider."""
        return {
            "label": self.meeting_code_label,
            "placeholder": self.meeting_code_placeholder,
            "hint": self.meeting_code_hint,
            "showBaseUrl": self.show_base_url,
        }


# Provider registry
_registry: dict[str, type[BaseProvider]] = {}
_metadata: dict[str, ProviderMetadata] = {}


def register_provider(
    name: str,
    provider_class: type[BaseProvider],
    metadata: ProviderMetadata | None = None,
) -> None:
    """Register a provider class.

    Args:
        name: Provider identifier (e.g., 'jitsi', 'webex')
        provider_class: Provider class to register
    """
    normalized_name = name.lower()
    _registry[normalized_name] = provider_class
    _metadata[normalized_name] = metadata or ProviderMetadata(
        name=normalized_name,
        label=normalized_name.title(),
        meeting_code_label="Meeting URL",
        meeting_code_placeholder="https://example.com/meeting",
        meeting_code_hint="Full meeting URL",
        show_base_url=False,
        badge_class="badge-ghost",
        telegram_url_hint="範例: https://example.com/meeting",
    )


def get_provider(provider_name: str) -> BaseProvider:
    """Get a provider instance by name.

    Args:
        provider_name: Provider identifier ('jitsi', 'webex', etc.)

    Returns:
        Provider instance

    Raises:
        ValueError: If provider is unknown
    """
    provider_class = _registry.get(provider_name.lower())
    if provider_class is None:
        available = ", ".join(_registry.keys()) or "(none)"
        raise ValueError(f"Unknown provider: {provider_name}. Available: {available}")
    return provider_class()


def list_providers() -> list[str]:
    """List all registered provider names.

    Returns:
        List of registered provider names
    """
    return list(_registry.keys())


def list_provider_metadata() -> list[ProviderMetadata]:
    """List metadata for all registered providers."""
    return [_metadata[name] for name in _registry.keys()]


def provider_form_config_map() -> dict[str, dict]:
    """Return browser form config keyed by provider name."""
    return {metadata.name: metadata.form_config() for metadata in list_provider_metadata()}


def get_provider_metadata(provider_name: str) -> ProviderMetadata:
    """Return metadata for a provider name."""
    normalized_name = validate_provider_name(provider_name)
    return _metadata[normalized_name]


def validate_provider_name(provider_name: str) -> str:
    """Normalize and validate a provider identifier."""
    normalized_name = provider_name.lower()
    if normalized_name not in _registry:
        available = ", ".join(_registry.keys()) or "(none)"
        raise ValueError(f"Unknown provider: {provider_name}. Available: {available}")
    return normalized_name


# Register built-in providers
register_provider(
    "jitsi",
    JitsiProvider,
    ProviderMetadata(
        name="jitsi",
        label="Jitsi",
        meeting_code_label="Meeting Code",
        meeting_code_placeholder="my-meeting-room",
        meeting_code_hint="Jitsi room name",
        show_base_url=True,
        badge_class="badge-info",
        telegram_url_hint="範例: https://meet.jit.si/your-meeting-room",
    ),
)
register_provider(
    "webex",
    WebexProvider,
    ProviderMetadata(
        name="webex",
        label="Webex",
        meeting_code_label="Meeting URL",
        meeting_code_placeholder="https://company.webex.com/meet/user",
        meeting_code_hint="Full Webex link",
        show_base_url=False,
        badge_class="badge-warning",
        telegram_url_hint="範例: https://xxx.webex.com/meet/your-room",
    ),
)
register_provider(
    "zoom",
    ZoomProvider,
    ProviderMetadata(
        name="zoom",
        label="Zoom",
        meeting_code_label="Meeting URL / ID",
        meeting_code_placeholder="https://zoom.us/j/123456789?pwd=...",
        meeting_code_hint="Full Zoom invite link is recommended",
        show_base_url=False,
        badge_class="badge-success",
        telegram_url_hint="範例: https://zoom.us/j/1234567890",
    ),
)


__all__ = [
    "BaseProvider",
    "JoinResult",
    "DiagnosticData",
    "MeetingState",
    "MeetingStateSnapshot",
    "ProviderMetadata",
    "JitsiProvider",
    "WebexProvider",
    "ZoomProvider",
    "get_provider",
    "get_provider_metadata",
    "register_provider",
    "list_providers",
    "list_provider_metadata",
    "provider_form_config_map",
    "validate_provider_name",
]
