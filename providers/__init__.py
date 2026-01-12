"""Meeting providers package.

This module provides a registry-based provider system for different meeting platforms.
New providers can be registered using `register_provider()`.
"""

from providers.base import BaseProvider, DiagnosticData, JoinResult
from providers.jitsi import JitsiProvider
from providers.webex import WebexProvider

# Provider registry
_registry: dict[str, type[BaseProvider]] = {}


def register_provider(name: str, provider_class: type[BaseProvider]) -> None:
    """Register a provider class.

    Args:
        name: Provider identifier (e.g., 'jitsi', 'webex')
        provider_class: Provider class to register
    """
    _registry[name.lower()] = provider_class


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


# Register built-in providers
register_provider("jitsi", JitsiProvider)
register_provider("webex", WebexProvider)


__all__ = [
    "BaseProvider",
    "JoinResult",
    "DiagnosticData",
    "JitsiProvider",
    "WebexProvider",
    "get_provider",
    "register_provider",
    "list_providers",
]
