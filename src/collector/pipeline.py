"""Phase 1 orchestration: channels -> uploads -> details -> normalised, stored records."""
from datetime import datetime, timezone
from itertools import zip_longest

from .fetch_details import fetch_video_details
from .models import CollectionCategory
from .normalize import normalize_video, unavailable_record
from .quota import QuotaExceeded
from .resolvers.channel import resolve_channel
from .sources.channel_uploads import ChannelUploadsSource
from .sources.random_search import make_query, random_window
from .storage import Storage
from .youtube_client import YouTubeClient, YouTubeError


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def collect_channels(
    category: CollectionCategory,
    channels: list[dict],
    client: YouTubeClient,
    storage: Storage,
    max_per_channel: int,
    run_id: str,
) -> dict:
    """Collect the newest `max_per_channel` videos of each channel. Never raises on one bad channel."""
    client.on_response = lambda name, params, resp: storage.write_raw(category, run_id, name, params, resp)
    summary = {"run_id": run_id, "category": category.value, "channels": [], "stopped_by_quota": False}

    for cfg in channels:
        row = {"name": cfg["name"], "url": cfg["url"], "collected": 0, "unavailable": 0, "error": None}
        summary["channels"].append(row)
        try:
            channel = resolve_channel(client, cfg["url"], cfg["name"])
            row.update(channel_id=channel["channel_id"], resolved_name=channel["channel_name"])
            if not channel["name_matches_expected"]:
                row["warning"] = f"resolved channel title '{channel['channel_name']}' differs from '{cfg['name']}'"

            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            source = {
                "category": category.value,
                "source_type": "channel_uploads",
                "source_id": channel["channel_id"],
                "source_name": cfg["name"],
                "channel_group": cfg.get("channel_group"),
                "run_id": run_id,
            }
            candidates = list(ChannelUploadsSource(client, channel, cfg["name"], max_per_channel).candidates())
            to_fetch = [c.video_id for c in candidates if not c.unavailable_reason]
            found = fetch_video_details(client, to_fetch)

            records, missing = [], []
            for cand in candidates:
                if cand.unavailable_reason:
                    missing.append(unavailable_record(cand.video_id, cand.unavailable_reason, category, source, fetched_at))
                elif cand.video_id in found:
                    records.append(normalize_video(found[cand.video_id], category, source, fetched_at))
                else:
                    missing.append(unavailable_record(
                        cand.video_id, "not_returned_by_api (deleted, private or blocked)", category, source, fetched_at))

            storage.upsert_videos(category, records)
            storage.write_unavailable(category, missing)
            row["collected"], row["unavailable"] = len(records), len(missing)
        except QuotaExceeded as err:
            row["error"] = str(err)
            summary["stopped_by_quota"] = True
            break
        except (YouTubeError, ValueError) as err:
            row["error"] = str(err)
    return summary


def collect_keywords(
    category: CollectionCategory,
    sections: dict[str, list[str]],
    client: YouTubeClient,
    storage: Storage,
    run_id: str,
    per_keyword: int,
    extra_source: dict | None = None,
    max_keywords: int | None = None,
    target_total: int | None = None,
) -> dict:
    """Search each not-yet-done keyword and keep its top `per_keyword` results.

    Resumable: a finished keyword is recorded, so re-running continues after a quota stop.
    A video already stored (any category) is merged, with this category and keyword added as a source.
    """
    client.on_response = lambda name, params, resp: storage.write_raw(category, run_id, name, params, resp)
    done = storage.done_keywords(category)
    row = {"name": "keyword_search", "collected": 0, "unavailable": 0, "keywords_done": 0,
           "keywords_remaining": 0, "error": None}
    summary = {"run_id": run_id, "category": category.value, "channels": [row], "stopped_by_quota": False}

    # Round-robin across sections so a stop (quota or target) never leaves a section uncovered.
    queues = [[(sec, kw) for kw in kws if kw not in done] for sec, kws in sections.items()]
    pending = [item for group in zip_longest(*queues) for item in group if item]
    row["keywords_remaining"] = len(pending)
    if max_keywords is not None:
        pending = pending[:max_keywords]

    try:
        for section, keyword in pending:
            if target_total and storage.counts()[category.folder] >= target_total:
                row["warning"] = f"target of {target_total} videos reached; remaining keywords not searched"
                break
            page = client.search_keyword(keyword, per_keyword)
            ids = [it["id"]["videoId"] for it in page.get("items", []) if it.get("id", {}).get("videoId")]
            ids = list(dict.fromkeys(ids))[:per_keyword]
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            source = {
                "category": category.value,
                "source_type": "keyword_search",
                "source_id": keyword,
                "source_name": section,
                "run_id": run_id,
                **(extra_source or {}),
            }
            found = fetch_video_details(client, ids) if ids else {}
            records = [normalize_video(found[i], category, source, fetched_at) for i in ids if i in found]
            storage.upsert_videos(category, records)
            storage.mark_keyword_done(category, keyword)
            row["collected"] += len(records)
            row["unavailable"] += len(ids) - len(records)
            row["keywords_done"] += 1
            row["keywords_remaining"] -= 1
    except QuotaExceeded as err:
        row["error"] = str(err)
        summary["stopped_by_quota"] = True
    except (YouTubeError, ValueError) as err:
        row["error"] = str(err)
    return summary


def collect_random(
    client: YouTubeClient,
    storage: Storage,
    run_id: str,
    words: list[str],
    rng,
    *,
    target: int,
    picks_per_search: int,
    max_searches: int,
    window_start,
    window_days: int,
) -> dict:
    """Top the random folder up to `target` videos. Skips IDs already stored in any category."""
    category = CollectionCategory.RANDOM_VIDEO
    client.on_response = lambda name, params, resp: storage.write_raw(category, run_id, name, params, resp)
    have = storage.counts()[category.folder]
    row = {"name": "random_search", "target": target, "collected": 0, "unavailable": 0, "searches": 0, "error": None}
    summary = {"run_id": run_id, "category": category.value, "channels": [row], "stopped_by_quota": False}
    seen = storage.known_ids()

    try:
        while have + row["collected"] < target and row["searches"] < max_searches:
            query = make_query(words, rng)
            after, before = random_window(window_start, window_days, rng)
            page = client.search_videos(query, after, before)
            row["searches"] += 1

            ids = [
                it["id"]["videoId"]
                for it in page.get("items", [])
                if it.get("id", {}).get("videoId") and it["id"]["videoId"] not in seen
            ]
            rng.shuffle(ids)
            ids = ids[: min(picks_per_search, target - have - row["collected"])]
            if not ids:
                continue
            seen.update(ids)

            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            source = {
                "category": category.value,
                "source_type": "random_search",
                "source_id": query,
                "source_name": f"{after} .. {before}",
                "run_id": run_id,
            }
            found = fetch_video_details(client, ids)
            records = [normalize_video(found[i], category, source, fetched_at) for i in ids if i in found]
            storage.upsert_videos(category, records)
            row["collected"] += len(records)
            row["unavailable"] += len(ids) - len(records)
    except QuotaExceeded as err:
        row["error"] = str(err)
        summary["stopped_by_quota"] = True
    except (YouTubeError, ValueError) as err:
        row["error"] = str(err)

    if not row["error"] and have + row["collected"] < target:
        row["warning"] = f"hit max_searches ({max_searches}) with {have + row['collected']} of {target} videos"
    return summary


def collect_popular(regions: dict[str, int], client: YouTubeClient, storage: Storage, run_id: str) -> dict:
    """Take the top `count` videos of each region's mostPopular chart.

    A video charting in several regions is stored once, with one source entry per region.
    """
    category = CollectionCategory.POPULAR_VIDEO
    client.on_response = lambda name, params, resp: storage.write_raw(category, run_id, name, params, resp)
    summary = {"run_id": run_id, "category": category.value, "channels": [], "stopped_by_quota": False}

    for region, count in regions.items():
        row = {"name": region, "target": count, "collected": 0, "unavailable": 0, "error": None}
        summary["channels"].append(row)
        try:
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            source = {
                "category": category.value,
                "source_type": "most_popular_chart",
                "source_id": region,
                "source_name": region,
                "run_id": run_id,
            }
            records, token = [], None
            while len(records) < count:
                page = client.most_popular(region, token)
                for item in page.get("items", []):
                    records.append(normalize_video(item, category, source, fetched_at))
                    if len(records) >= count:
                        break
                token = page.get("nextPageToken")
                if not token:
                    break
            storage.upsert_videos(category, records)
            row["collected"] = len(records)
            if len(records) < count:
                row["warning"] = f"chart returned only {len(records)} of {count} requested videos"
        except QuotaExceeded as err:
            row["error"] = str(err)
            summary["stopped_by_quota"] = True
            break
        except (YouTubeError, ValueError) as err:
            row["error"] = str(err)
    return summary
