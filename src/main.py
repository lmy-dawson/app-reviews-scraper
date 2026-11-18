"""
App Store & Google Play Reviews Scraper.

Give it app links or IDs from either store and it returns the reviews, one row
each, in the same shape for both stores: rating, title, text, date, version,
developer reply. Any storefront country, any sort order, optional star-rating
and date filters.

Nothing here needs a login, an API key or a cookie. Apple rate-limits its
review endpoint per address, so App Store requests are paced and, by default,
spread over Apify's shared datacenter proxy, which every plan includes at no
extra charge. Google Play is tolerant and is read directly.

Billing is done by the platform, not by this code. The Actor is priced on
Apify's built-in events: a fixed fee when a run starts, and one charge for
every item written to the default dataset. The default dataset is therefore
the bill, and the rule this file keeps is that only a real review is ever
pushed there. Apps that fail or return nothing go to the key-value store
instead, where they cost the user nothing, which is what the README promises.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from apify import Actor

from . import apple, google
from .inputs import APP_STORE, GOOGLE_PLAY, AppRef, parse_app, parse_ratings, parse_since

FAILED_KEY = "APPS_WITHOUT_REVIEWS"
DETAILS_KEY = "APP_DETAILS"
PUSH_BATCH = 100
APPLE_CONCURRENCY = 2
GOOGLE_CONCURRENCY = 4
SCAN_HARD_CAP = 50_000


@dataclass
class Config:
    max_reviews: int
    language: str
    sort: str
    ratings: set[int]
    since: datetime | None

    @property
    def filtering(self) -> bool:
        return bool(self.ratings or self.since)

    def scan_cap(self) -> int:
        # A rating filter on the App Store has to be applied after the fact,
        # so bound how far past the request we are willing to read.
        if not self.filtering:
            return SCAN_HARD_CAP
        return min(max(self.max_reviews * 10, 200), SCAN_HARD_CAP)


@dataclass
class Job:
    ref: AppRef
    country: str
    app: dict | None = None
    pushed: int = 0
    scanned: int = 0
    outcome: str = ""
    blocked: bool = False
    seen: set = field(default_factory=set)

    @property
    def label(self) -> str:
        return f"{self.ref.label} [{self.country}]"


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _review_date(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(row["date"])
    except (KeyError, TypeError, ValueError):
        return None


def _passes(row: dict, cfg: Config) -> bool:
    if cfg.ratings and row.get("rating") not in cfg.ratings:
        return False
    if cfg.since:
        when = _review_date(row)
        if when is not None and when < cfg.since:
            return False
    return True


def _older_than_since(row: dict, cfg: Config) -> bool:
    if not cfg.since:
        return False
    when = _review_date(row)
    return when is not None and when < cfg.since


APPLE_GAP_RETRIES = 3


async def _apple_rows(client: apple.AppleClient, job: Job, cfg: Config, scraped_at: str):
    offset = 0
    gaps = 0
    while True:
        try:
            items, has_next = await apple.fetch_page(client, job.ref.app_id, job.country, cfg.sort, offset)
        except apple.Gap:
            gaps += 1
            if offset == 0:
                if gaps >= APPLE_GAP_RETRIES:
                    raise apple.AppNotFound("not in this storefront, or has no reviews")
                await asyncio.sleep(1.5)
                continue
            if gaps >= APPLE_GAP_RETRIES:
                return
            # A hole in the listing: step over it and carry on.
            offset += apple.PAGE_SIZE
            await asyncio.sleep(client.pace)
            continue
        gaps = 0
        for item in items:
            yield apple.normalize(item, job.app, job.country, cfg.language, scraped_at)
        if not items or not has_next:
            return
        offset += len(items)
        await asyncio.sleep(client.pace)


async def _google_rows(job: Job, cfg: Config, scraped_at: str):
    if cfg.ratings:
        scores: list[int | None] = sorted(cfg.ratings)
    elif cfg.sort == "lowest_rating":
        scores = [1, 2, 3, 4, 5]
    else:
        scores = [None]
    for score in scores:
        token = None
        while True:
            items, token = await google.fetch_batch(
                job.ref.app_id, job.country, cfg.language, cfg.sort, score, token, google.BATCH
            )
            for item in items:
                yield google.normalize(item, job.app, job.country, cfg.language, scraped_at)
            if not items or getattr(token, "token", None) is None:
                break


async def _collect(job: Job, cfg: Config, rows) -> None:
    buffer: list[dict] = []
    cap = cfg.scan_cap()
    try:
        async for row in rows:
            job.scanned += 1
            if cfg.sort == "newest" and _older_than_since(row, cfg):
                job.outcome = "reached reviews older than the date you asked for"
                break
            key = row.get("reviewId") or f"{row.get('userName')}|{row.get('date')}"
            if key in job.seen:
                continue
            job.seen.add(key)
            if not _passes(row, cfg):
                if job.scanned >= cap:
                    job.outcome = f"stopped after reading {job.scanned} reviews looking for matches"
                    break
                continue
            buffer.append(row)
            job.pushed += 1
            if len(buffer) >= PUSH_BATCH:
                await Actor.push_data(buffer)
                buffer = []
            if job.pushed >= cfg.max_reviews:
                job.outcome = "reached max reviews"
                break
        else:
            job.outcome = "no more reviews in the store"
    finally:
        if buffer:
            await Actor.push_data(buffer)


async def _run_apple(job: Job, cfg: Config, scraped_at: str, proxy_configuration, sem: asyncio.Semaphore) -> None:
    async with sem:
        try:
            async with apple.AppleClient(proxy_configuration) as client:
                job.app = await apple.lookup(client, job.ref.app_id, job.country) or {
                    "store": APP_STORE,
                    "appId": job.ref.app_id,
                    "country": job.country,
                    "appName": None,
                    "appUrl": apple.APP_URL.format(country=job.country, app_id=job.ref.app_id),
                }
                Actor.log.info(f"{job.label}: {job.app.get('appName') or 'name unknown'}")
                await _collect(job, cfg, _apple_rows(client, job, cfg, scraped_at))
        except apple.AppNotFound as error:
            job.outcome = f"not found: {error}"
        except apple.Blocked as error:
            job.blocked = True
            job.outcome = f"App Store rate limit, try again in a few minutes ({error})"
        except Exception as error:  # noqa: BLE001 - one bad app must not kill the run
            job.outcome = f"error: {type(error).__name__}: {error}"
            Actor.log.exception(f"{job.label} failed")
        Actor.log.info(f"{job.label}: {job.pushed} reviews ({job.outcome})")


async def _run_google(job: Job, cfg: Config, scraped_at: str, sem: asyncio.Semaphore) -> None:
    async with sem:
        try:
            job.app = await google.details(job.ref.app_id, job.country, cfg.language) or {
                "store": GOOGLE_PLAY,
                "appId": job.ref.app_id,
                "country": job.country,
                "appName": None,
                "appUrl": google.APP_URL.format(app_id=job.ref.app_id, language=cfg.language, country=job.country),
            }
            Actor.log.info(f"{job.label}: {job.app.get('appName') or 'name unknown'}")
            await _collect(job, cfg, _google_rows(job, cfg, scraped_at))
        except google.AppNotFound as error:
            job.outcome = f"not found: {error}"
        except Exception as error:  # noqa: BLE001 - one bad app must not kill the run
            job.outcome = f"error: {type(error).__name__}: {error}"
            Actor.log.exception(f"{job.label} failed")
        Actor.log.info(f"{job.label}: {job.pushed} reviews ({job.outcome})")


async def _proxy_configuration(proxy_input: dict | None):
    if not proxy_input or not (proxy_input.get("useApifyProxy") or proxy_input.get("proxyUrls")):
        return None
    try:
        return await Actor.create_proxy_configuration(actor_proxy_input=proxy_input)
    except Exception as error:  # noqa: BLE001 - the stores work without one
        Actor.log.warning(f"Proxy could not be configured, continuing without one: {error}")
        return None


async def main() -> None:
    async with Actor:
        inp = await Actor.get_input() or {}

        refs: list[AppRef] = []
        failed: list[dict] = []
        for raw in inp.get("apps") or []:
            ref = parse_app(str(raw))
            if ref is None:
                failed.append({"input": raw, "reason": "not an App Store or Google Play link, ID or package name"})
            else:
                refs.append(ref)

        countries = [c.strip().lower() for c in inp.get("countries") or [] if str(c).strip()] or ["us"]
        cfg = Config(
            max_reviews=max(1, _int(inp.get("maxReviewsPerApp"), 500)),
            language=(str(inp.get("language") or "en").strip().lower() or "en"),
            sort=str(inp.get("sort") or "newest"),
            ratings=parse_ratings(inp.get("ratings")),
            since=parse_since(inp.get("reviewsSince")),
        )
        if inp.get("reviewsSince") and cfg.since is None:
            Actor.log.warning(f"Could not read the date {inp.get('reviewsSince')!r}; ignoring it. Use YYYY-MM-DD.")

        jobs: list[Job] = []
        for ref in refs:
            for country in ([ref.country] if ref.country else countries):
                jobs.append(Job(ref=ref, country=country))

        if not jobs:
            await Actor.set_value(FAILED_KEY, failed)
            await Actor.fail(status_message="No usable app links or IDs in the input.")
            return

        Actor.log.info(
            f"{len(jobs)} app/country pairs, up to {cfg.max_reviews} reviews each, "
            f"sort={cfg.sort}, ratings={sorted(cfg.ratings) or 'all'}, since={cfg.since.date() if cfg.since else 'any'}"
        )

        proxy_input = inp.get("proxyConfiguration") or {}
        proxy_configuration = await _proxy_configuration(proxy_input)
        if proxy_configuration is not None and proxy_input.get("proxyUrls"):
            # Only a proxy the user chose on purpose is applied to Google Play.
            # google-play-scraper uses urllib, which reads the proxy from the environment.
            url = await proxy_configuration.new_url()
            os.environ["HTTP_PROXY"] = url
            os.environ["HTTPS_PROXY"] = url

        scraped_at = datetime.now(timezone.utc).isoformat()
        apple_sem = asyncio.Semaphore(APPLE_CONCURRENCY)
        google_sem = asyncio.Semaphore(GOOGLE_CONCURRENCY)
        await asyncio.gather(
            *(
                _run_apple(job, cfg, scraped_at, proxy_configuration, apple_sem)
                if job.ref.store == APP_STORE
                else _run_google(job, cfg, scraped_at, google_sem)
                for job in jobs
            )
        )

        total = sum(job.pushed for job in jobs)
        for job in jobs:
            if job.pushed == 0:
                failed.append({"input": job.ref.raw, "store": job.ref.store, "appId": job.ref.app_id, "country": job.country, "reason": job.outcome})
        await Actor.set_value(DETAILS_KEY, [job.app for job in jobs if job.app])
        await Actor.set_value(FAILED_KEY, failed)

        Actor.log.info("Summary:")
        for job in jobs:
            Actor.log.info(f"  {job.label:<40} {job.pushed:>7} reviews  {job.outcome}")
        if failed:
            Actor.log.info(f"  {len(failed)} input(s) returned nothing; see the {FAILED_KEY} record for reasons.")

        blocked = [job for job in jobs if job.blocked]
        if total == 0 and blocked:
            await Actor.fail(status_message="The App Store rate limit blocked every request. Try again in a few minutes.")
            return
        await Actor.set_status_message(
            f"{total} reviews from {sum(1 for j in jobs if j.pushed)} of {len(jobs)} app/country pairs"
        )
