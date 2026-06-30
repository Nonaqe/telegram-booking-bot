"""Timezone helper with a fallback for Python 3.8 (no stdlib zoneinfo)."""

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover - 3.8 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore

__all__ = ["ZoneInfo"]
