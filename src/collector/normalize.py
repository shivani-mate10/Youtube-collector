"""Raw API items -> clean records. Hidden or missing values become None, never 0."""
import isodate

from .models import AnnotationStatus, AvailabilityStatus, CollectionCategory

_THUMB_ORDER = ("maxres", "standard", "high", "medium", "default")


def _int_or_none(value) -> int | None:
    return int(value) if value is not None else None


def _thumbnail(thumbnails: dict) -> str | None:
    for key in _THUMB_ORDER:
        if key in thumbnails:
            return thumbnails[key]["url"]
    return None


def _duration_seconds(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(isodate.parse_duration(iso).total_seconds())
    except (ValueError, isodate.ISO8601Error):
        return None


def normalize_video(item: dict, category: CollectionCategory, source: dict, fetched_at: str) -> dict:
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    content = item.get("contentDetails", {})
    vid = item["id"]
    return {
        "video_id": vid,
        "video_url": f"https://www.youtube.com/watch?v={vid}",
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "thumbnail_url": _thumbnail(snippet.get("thumbnails", {})),
        "channel_id": snippet.get("channelId"),
        "channel_name": snippet.get("channelTitle"),
        "published_at": snippet.get("publishedAt"),
        "tags": snippet.get("tags", []),
        "view_count": _int_or_none(stats.get("viewCount")),
        "like_count": _int_or_none(stats.get("likeCount")),
        "comment_count": _int_or_none(stats.get("commentCount")),
        "duration_seconds": _duration_seconds(content.get("duration")),
        "duration_iso": content.get("duration"),
        "live_broadcast_content": snippet.get("liveBroadcastContent"),
        "made_for_kids": item.get("status", {}).get("madeForKids"),
        "collection_category": category.value,
        "collection_date": fetched_at[:10],
        "last_fetched_at": fetched_at,
        "annotation_status": AnnotationStatus.PENDING.value,
        "availability_status": AvailabilityStatus.AVAILABLE.value,
        "source_categories": [category.value],
        "sources": [source],
    }


def unavailable_record(video_id: str, reason: str, category: CollectionCategory, source: dict, fetched_at: str) -> dict:
    return {
        "video_id": video_id,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "collection_category": category.value,
        "availability_status": AvailabilityStatus.UNAVAILABLE.value,
        "unavailable_reason": reason,
        "detected_at": fetched_at,
        "source": source,
    }
