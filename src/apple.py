"""
App Store reviews.

The App Store website is a server-rendered app that fetches its review pages
from Apple's media API. Calling that API directly needs a bearer token that
Apple no longer prints into the page, but the website also exposes a
same-origin proxy, `apps.apple.com/api/apps/...`, which attaches the token on
Apple's side. That proxy answers plain requests with no cookie, token or
login, serves 20 reviews per request, and paginates by offset as deep as the
listing goes (offset 10,000 still returned a page in testing).

Apple rate-limits that proxy per source address. From one address, about a
hundred requests in a minute were fine, but a second burst a minute later
was answered with HTTP 429 for half a minute. Datacenter addresses are shared
by many tenants, so the budget can already be spent when a run starts. The
client below therefore paces its requests and, when a proxy configuration is
available, answers a 429 by switching to a fresh proxy session, which means a
fresh address. Without a proxy it waits out `Retry-After` before giving up.

The older customer-reviews RSS feed still answers 200 but has carried no
entries since 2026, so it is not used.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone

import httpx
from apify import Actor

PAGE_SIZE = 20
REVIEWS_URL = "https://apps.apple.com/api/apps/v1/catalog/{country}/apps/{app_id}/reviews"
LOOKUP_URL = "https://itunes.apple.com/lookup"
APP_URL = "https://apps.apple.com/{country}/app/id{app_id}"

SORT_PARAM = {
    "newest": "recent",
    "most_helpful": "helpful",
    "highest_rating": "favorable",
    "lowest_rating": "critical",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://apps.apple.com",
    "Referer": "https://apps.apple.com/",
}

SERVER_ERRORS = {500, 502, 503, 504}
MAX_ATTEMPTS = 12
MAX_ROTATIONS = 20
MAX_WAIT = 60.0
PACE_MIN = 0.25
PACE_MAX = 2.0
CLEAN_BEFORE_SPEEDUP = 20


class AppNotFound(Exception):
    pass


class Blocked(Exception):
    pass


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class AppleClient:
    """One HTTP client per job, so a proxy rotation never disturbs another job."""

    def __init__(self, proxy_configuration=None, timeout: float = 30.0):
        self._proxy_configuration = proxy_configuration
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._session = 0
        self._clean = 0
        self.rotations = 0
        # Seconds to wait between pages. Doubles on every rate limit and
        # halves again after a run of clean responses, so a job settles at
        # whatever rate Apple is willing to serve right now.
        self.pace = PACE_MIN

    def _slower(self) -> None:
        self.pace = min(self.pace * 2, PACE_MAX)
        self._clean = 0

    def _clean_response(self) -> None:
        self._clean += 1
        if self._clean >= CLEAN_BEFORE_SPEEDUP and self.pace > PACE_MIN:
            self.pace = max(self.pace / 2, PACE_MIN)
            self._clean = 0

    async def __aenter__(self):
        await self._open()
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def _open(self) -> None:
        await self.close()
        proxy_url = None
        if self._proxy_configuration is not None:
            self._session += 1
            session_id = f"apple{random.randint(0, 999999):06d}s{self._session}"
            try:
                proxy_url = await self._proxy_configuration.new_url(session_id=session_id)
            except Exception as error:  # noqa: BLE001 - the store works without one
                Actor.log.warning(f"Could not get a proxy URL, continuing direct: {error}")
        self._client = httpx.AsyncClient(
            headers=HEADERS, proxy=proxy_url, timeout=self._timeout, follow_redirects=True
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def can_rotate(self) -> bool:
        return self._proxy_configuration is not None and self.rotations < MAX_ROTATIONS

    async def get(self, url: str, params: dict) -> httpx.Response:
        delay = 5.0
        last: Exception | None = None
        for _ in range(MAX_ATTEMPTS):
            try:
                response = await self._client.get(url, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as error:
                last = error
                if self.can_rotate:
                    self.rotations += 1
                    await self._open()
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_WAIT)
                continue

            if response.status_code == 429:
                last = Blocked("HTTP 429, App Store rate limit")
                self._slower()
                if self.can_rotate:
                    self.rotations += 1
                    Actor.log.warning(
                        f"App Store rate limit hit, switching to a fresh proxy session (pace now {self.pace:.2f}s)"
                    )
                    await self._open()
                    await asyncio.sleep(self.pace)
                    continue
                wait = min(_retry_after(response) or delay, MAX_WAIT)
                Actor.log.warning(f"App Store rate limit hit, waiting {wait:.0f}s")
                await asyncio.sleep(wait)
                delay = min(delay * 2, MAX_WAIT)
                continue

            if response.status_code in SERVER_ERRORS:
                last = Blocked(f"HTTP {response.status_code}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_WAIT)
                continue

            self._clean_response()
            return response
        raise Blocked(f"gave up after {MAX_ATTEMPTS} attempts: {last}")


async def lookup(client: AppleClient, app_id: str, country: str) -> dict | None:
    """Public iTunes lookup. Free, stable for a decade, no auth."""
    try:
        response = await client.get(LOOKUP_URL, {"id": app_id, "country": country})
        data = response.json()
    except Exception as error:  # noqa: BLE001 - details are optional
        Actor.log.warning(f"App Store lookup failed for {app_id} ({country}): {error}")
        return None
    results = data.get("results") or []
    if not results:
        return None
    r = results[0]
    return {
        "store": "app_store",
        "appId": app_id,
        "country": country,
        "appName": r.get("trackName"),
        "appUrl": r.get("trackViewUrl") or APP_URL.format(country=country, app_id=app_id),
        "bundleId": r.get("bundleId"),
        "developer": r.get("sellerName") or r.get("artistName"),
        "developerUrl": r.get("sellerUrl") or r.get("artistViewUrl"),
        "category": r.get("primaryGenreName"),
        "categories": r.get("genres"),
        "averageRating": r.get("averageUserRating"),
        "ratingCount": r.get("userRatingCount"),
        "version": r.get("version"),
        "released": r.get("releaseDate"),
        "updated": r.get("currentVersionReleaseDate"),
        "price": r.get("price"),
        "currency": r.get("currency"),
        "contentRating": r.get("contentAdvisoryRating"),
        "minimumOs": r.get("minimumOsVersion"),
        "sizeBytes": int(r["fileSizeBytes"]) if str(r.get("fileSizeBytes") or "").isdigit() else None,
        "icon": r.get("artworkUrl512") or r.get("artworkUrl100"),
    }


class Gap(Exception):
    """Apple answered 404 'No related resources' for this offset.

    That happens sporadically in the middle of a listing (offset 2,000 failed
    while 5,000 succeeded in testing), so it is a hole to step over, not the
    end of the reviews. Only a 404 that repeats at offset 0 means the app is
    not in that storefront.
    """


async def fetch_page(
    client: AppleClient, app_id: str, country: str, sort: str, offset: int
) -> tuple[list[dict], bool]:
    """One page of raw review objects and whether Apple says there is a next one."""
    params = {
        "platform": "iphone",
        "additionalPlatforms": "appletv,ipad,iphone,mac",
        "sort": SORT_PARAM.get(sort, "recent"),
        "offset": offset,
        "limit": PAGE_SIZE,
    }
    response = await client.get(REVIEWS_URL.format(country=country, app_id=app_id), params)
    if response.status_code == 404:
        raise Gap(f"offset {offset}")
    if response.status_code != 200:
        raise Blocked(f"HTTP {response.status_code} at offset {offset}")
    data = response.json()
    items = data.get("data") or []
    return items, bool(data.get("next"))


def _iso(value) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return str(value)


def normalize(item: dict, app: dict, country: str, language: str, scraped_at: str) -> dict:
    a = item.get("attributes") or {}
    reply = a.get("developerResponse") or {}
    return {
        "store": "app_store",
        "appId": app["appId"],
        "appName": app.get("appName"),
        "appUrl": app.get("appUrl"),
        "country": country,
        "language": language,
        "reviewId": str(item.get("id") or ""),
        "userName": a.get("userName"),
        "rating": a.get("rating"),
        "title": a.get("title"),
        "text": a.get("review"),
        "date": _iso(a.get("date")),
        "appVersion": None,
        "isEdited": bool(a.get("isEdited")),
        "thumbsUpCount": None,
        "developerResponse": reply.get("body") or None,
        "developerResponseDate": _iso(reply.get("modified")),
        "scrapedAt": scraped_at,
    }
