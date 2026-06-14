"""Shared service-layer exceptions."""


class ServiceError(Exception):
    """Base class for service-layer failures."""


class NotFoundError(ServiceError):
    """Raised when a requested domain object does not exist."""


class ValidationError(ServiceError):
    """Raised when a request is invalid for the domain operation."""


class ConflictError(ServiceError):
    """Raised when an operation conflicts with current runtime state."""
