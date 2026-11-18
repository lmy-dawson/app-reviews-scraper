"""
Turn whatever the user pasted into (store, app id, country).

People paste App Store links with or without a country, iTunes links from old
bookmarks, bare numeric IDs with or without the `id` prefix, Google Play links
with tracking parameters, and bare package names. All of those should just
work, and anything else should be reported by name rather than silently
dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

APP_STORE = "app_store"
GOOGLE_PLAY = "google_play"

_APPLE_URL = re.compile(
    r"(?:apps|itunes)\.apple\.com/(?:(?P<cc>[a-z]{2})/)?[^?#]*?/id(?P<id>\d+)", re.I
)
_APPLE_ID = re.compile(r"^(?:id)?(?P<id>\d{6,})$", re.I)
_PLAY_URL = re.compile(r"play\.google\.com/store/apps/details\?[^#]*?\bid=(?P<id>[A-Za-z0-9_.]+)", re.I)
_PLAY_GL = re.compile(r"[?&]gl=(?P<cc>[a-zA-Z]{2})\b")
_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")


@dataclass(frozen=True)
class AppRef:
    store: str
    app_id: str
    country: str | None
    raw: str

    @property
    def label(self) -> str:
        return f"{'App Store' if self.store == APP_STORE else 'Google Play'} {self.app_id}"


def parse_app(raw: str) -> AppRef | None:
    text = (raw or "").strip()
    if not text:
        return None
    match = _APPLE_URL.search(text)
    if match:
        country = (match.group("cc") or "").lower() or None
        return AppRef(APP_STORE, match.group("id"), country, raw)
    match = _PLAY_URL.search(text)
    if match:
        gl = _PLAY_GL.search(text)
        return AppRef(GOOGLE_PLAY, match.group("id"), gl.group("cc").lower() if gl else None, raw)
    match = _APPLE_ID.match(text)
    if match:
        return AppRef(APP_STORE, match.group("id"), None, raw)
    if _PACKAGE.match(text):
        return AppRef(GOOGLE_PLAY, text, None, raw)
    return None


def parse_since(value) -> datetime | None:
    """Accept `2026-01-01`, a full ISO timestamp, or nothing."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_ratings(values) -> set[int]:
    ratings: set[int] = set()
    for value in values or []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= 5:
            ratings.add(number)
    return ratings
