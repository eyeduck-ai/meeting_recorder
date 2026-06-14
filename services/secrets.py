SECRET_MASK = "********"


def mask_secret(value: str | None) -> str:
    """Return the UI/API-safe representation for a stored secret."""
    return SECRET_MASK if value else ""


def is_masked_secret(value: str | None) -> bool:
    """Return whether a submitted value is the unchanged secret sentinel."""
    return value == SECRET_MASK


def preserve_masked_secret(value: str | None, existing_value: str | None) -> str | None:
    """Keep the existing secret when a masked sentinel is submitted."""
    if is_masked_secret(value):
        return existing_value or ""
    return value
