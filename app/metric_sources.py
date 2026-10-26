"""Every metric source in one place (§102): who may write it, for which unit, how fresh it must be.

A new reporter (Search Console, AdSense, KDP) is one entry here, not a new special case in
the API, the freshness check and the Desk. `writers` are api_auth token names; an empty set
means internal only (written in-process, refused on the HTTP endpoint to every caller).
Trusted host callers (local, host, admin) may write any non-internal source.
"""
from datetime import timedelta

DEFAULT_FRESHNESS = timedelta(days=8)

SOURCES = {
    # Polar syncs hourly (cron :17) inside company-api: app/polar_metrics.py.
    "polar-api": {"units": {"seerxo"}, "writers": set(), "freshness": timedelta(hours=3)},
    # Seerxo's activation funnel, pulled hourly (:27) from seerxo-backend: app/seerxo_funnel.py.
    "seerxo-backend": {"units": {"seerxo"}, "writers": set(), "freshness": timedelta(hours=3)},
    # youtube-auto reports once per daily production run; one missed day is tolerated
    # (the watchdog already pages a missed publish at 48 h).
    "youtube-data-api": {"units": {"youtube"}, "writers": {"youtube"}, "freshness": timedelta(hours=72)},
    "youtube-auto": {"units": {"youtube"}, "writers": {"youtube"}, "freshness": timedelta(hours=72)},
    # Watch behaviour (CTR, view duration, 30 s retention), 28-day windows, from the YouTube
    # Analytics API in the same daily run (youtube-auto performans-topla.js).
    "youtube-analytics-api": {"units": {"youtube"}, "writers": {"youtube"}, "freshness": timedelta(hours=72)},
}


def freshness(source: str | None) -> timedelta:
    return SOURCES.get(source or "", {}).get("freshness", DEFAULT_FRESHNESS)


def internal_only(source: str) -> bool:
    return source in SOURCES and not SOURCES[source]["writers"]


def writable(caller: str) -> tuple[set[str], set[str]]:
    """(units, sources) a token caller may write."""
    mine = {s: v for s, v in SOURCES.items() if caller in v["writers"]}
    return set().union(*(v["units"] for v in mine.values())), set(mine)
