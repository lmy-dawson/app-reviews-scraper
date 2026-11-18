"""
Google Play reviews.

Google Play has no review API. The store website loads reviews through its
internal `batchexecute` RPC, which answers plain requests with no cookie or
login and is tolerant of datacenter addresses. The `google-play-scraper`
package speaks that RPC and has tracked its changes for years, so it is used
here rather than re-deriving the wire format. It is synchronous, so every call
runs in a worker thread.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from apify import Actor
from google_play_scraper import Sort, app as gp_app, reviews as gp_reviews
from google_play_scraper.exceptions import NotFoundError

APP_URL = "https://play.google.com/store/apps/details?id={app_id}&hl={language}&gl={country}"
BATCH = 200

SORT_PARAM = {
    "newest": Sort.NEWEST,
    "most_helpful": Sort.MOST_RELEVANT,
    "highest_rating": Sort.RATING,
    # Lowest rating has no native sort; it is served by walking scores 1..5.
    "lowest_rating": Sort.NEWEST,
}


class AppNotFound(Exception):
    pass


async def details(app_id: str, country: str, language: str) -> dict | None:
    try:
        r = await asyncio.to_thread(gp_app, app_id, lang=language, country=country)
    except NotFoundError as error:
        raise AppNotFound(str(error)) from error
    except Exception as error:  # noqa: BLE001 - details are optional
        Actor.log.warning(f"Google Play details failed for {app_id} ({country}): {error}")
        return None
    return {
        "store": "google_play",
        "appId": app_id,
        "country": country,
        "appName": r.get("title"),
        "appUrl": r.get("url") or APP_URL.format(app_id=app_id, language=language, country=country),
        "developer": r.get("developer"),
        "developerUrl": r.get("developerWebsite"),
        "developerEmail": r.get("developerEmail"),
        "category": r.get("genre"),
        "categories": [c.get("name") for c in (r.get("categories") or []) if isinstance(c, dict)],
        "averageRating": r.get("score"),
        "ratingCount": r.get("ratings"),
        "reviewCount": r.get("reviews"),
        "installs": r.get("installs"),
        "minInstalls": r.get("minInstalls"),
        "version": r.get("version"),
        "released": r.get("released"),
        "updated": _epoch(r.get("updated")),
        "price": r.get("price"),
        "currency": r.get("currency"),
        "free": r.get("free"),
        "containsAds": r.get("containsAds"),
        "offersInAppPurchases": r.get("offersIAP"),
        "contentRating": r.get("contentRating"),
        "icon": r.get("icon"),
    }


async def fetch_batch(
    app_id: str, country: str, language: str, sort: str, score: int | None, token, count: int
) -> tuple[list[dict], object]:
    """Up to `count` raw reviews plus the continuation token for the next call."""
    try:
        return await asyncio.to_thread(
            gp_reviews,
            app_id,
            lang=language,
            country=country,
            sort=SORT_PARAM.get(sort, Sort.NEWEST),
            count=min(max(count, 1), BATCH),
            filter_score_with=score,
            continuation_token=token,
        )
    except NotFoundError as error:
        raise AppNotFound(str(error)) from error


def _epoch(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _when(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def normalize(item: dict, app: dict, country: str, language: str, scraped_at: str) -> dict:
    return {
        "store": "google_play",
        "appId": app["appId"],
        "appName": app.get("appName"),
        "appUrl": app.get("appUrl"),
        "country": country,
        "language": language,
        "reviewId": item.get("reviewId"),
        "userName": item.get("userName"),
        "rating": item.get("score"),
        "title": None,
        "text": item.get("content"),
        "date": _when(item.get("at")),
        "appVersion": item.get("reviewCreatedVersion") or item.get("appVersion"),
        "isEdited": None,
        "thumbsUpCount": item.get("thumbsUpCount"),
        "developerResponse": item.get("replyContent") or None,
        "developerResponseDate": _when(item.get("repliedAt")),
        "scrapedAt": scraped_at,
    }
