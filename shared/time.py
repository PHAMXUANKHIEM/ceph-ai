"""UTC clock helpers used by production code.

The application currently stores legacy timestamps in timezone-naive database
columns.  ``utc_now()`` therefore returns a naive UTC value for wire/schema
compatibility while obtaining it through the non-deprecated timezone-aware
clock API.  The schema migration to ``TIMESTAMP WITH TIME ZONE`` can then be
done separately without mixing data conversion with this warning cleanup.
"""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current UTC instant in the legacy naive-UTC representation."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
