"""Parsing app links and IDs from either store."""
from datetime import timezone

import pytest

from src.inputs import APP_STORE, GOOGLE_PLAY, parse_app, parse_ratings, parse_since


@pytest.mark.parametrize("raw,store,app_id,country", [
    ("https://apps.apple.com/gb/app/spotify-music/id324684580", APP_STORE, "324684580", "gb"),
    ("https://apps.apple.com/app/id324684580", APP_STORE, "324684580", None),
    ("https://itunes.apple.com/us/app/whatsapp/id310633997?mt=8", APP_STORE, "310633997", "us"),
    ("id310633997", APP_STORE, "310633997", None),
    ("310633997", APP_STORE, "310633997", None),
    ("https://play.google.com/store/apps/details?id=com.spotify.music&hl=en&gl=DE",
     GOOGLE_PLAY, "com.spotify.music", "de"),
    ("https://play.google.com/store/apps/details?hl=en&id=com.whatsapp",
     GOOGLE_PLAY, "com.whatsapp", None),
    ("com.instagram.android", GOOGLE_PLAY, "com.instagram.android", None),
])
def test_parse_app(raw, store, app_id, country):
    ref = parse_app(raw)
    assert (ref.store, ref.app_id, ref.country) == (store, app_id, country)


@pytest.mark.parametrize("raw", ["", "   ", "spotify", "12345", None])
def test_unparseable_input_returns_none(raw):
    assert parse_app(raw) is None


def test_label_names_the_store():
    assert parse_app("com.whatsapp").label == "Google Play com.whatsapp"
    assert parse_app("id310633997").label == "App Store 310633997"


def test_parse_since_defaults_to_utc():
    assert parse_since("2026-01-01").tzinfo == timezone.utc
    assert parse_since("2026-01-01T10:00:00Z").hour == 10
    assert parse_since("") is None
    assert parse_since("not a date") is None


def test_parse_ratings_keeps_only_one_to_five():
    assert parse_ratings(["1", 5, "9", "x", None, 3]) == {1, 3, 5}
    assert parse_ratings(None) == set()
