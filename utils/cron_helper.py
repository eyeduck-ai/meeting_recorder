"""Utility functions for cron expression handling."""

from cron_descriptor import Options, get_description


def cron_to_chinese(expression: str) -> str:
    """Convert cron expression to human-readable Chinese.

    Args:
        expression: A cron expression string (e.g., "0 9 * * 1-5")

    Returns:
        Human-readable description in Chinese (e.g., "在 09:00, 週一 至 週五")
    """
    if not expression:
        return ""

    try:
        options = Options()
        options.locale_code = "en_US"
        desc = get_description(expression, options)
        # Manual shortener for days
        replacements = {
            "Monday": "Mon",
            "Tuesday": "Tue",
            "Wednesday": "Wed",
            "Thursday": "Thu",
            "Friday": "Fri",
            "Saturday": "Sat",
            "Sunday": "Sun",
            "At": "@",
            "only on": "",
        }
        for k, v in replacements.items():
            desc = desc.replace(k, v)
        return desc
    except Exception:
        # Fallback to original expression if parsing fails
        return expression
