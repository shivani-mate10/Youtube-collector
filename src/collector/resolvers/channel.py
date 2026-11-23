"""Turn a channel URL into channel ID, name and uploads playlist."""
import re
from urllib.parse import urlparse

from ..youtube_client import NotFound, YouTubeClient

_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")


def parse_channel_url(url: str) -> tuple[str, str]:
    """Return ("id", "UC...") or ("handle", "name"). Query strings like ?si=... are ignored."""
    parts = [p for p in urlparse(url.strip()).path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "channel" and _CHANNEL_ID.match(parts[1]):
        return "id", parts[1]
    if parts and parts[0].startswith("@") and len(parts[0]) > 1:
        return "handle", parts[0]
    raise ValueError(f"Unsupported channel URL: {url}")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def resolve_channel(client: YouTubeClient, url: str, expected_name: str | None = None) -> dict:
    kind, value = parse_channel_url(url)
    item = client.channel_by_id(value) if kind == "id" else client.channel_by_handle(value)
    if item is None:
        raise NotFound(f"No channel found for {url}")
    title = item["snippet"]["title"]
    name_matches = True
    if expected_name:
        a, b = _norm(expected_name), _norm(title)
        name_matches = bool(a) and bool(b) and (a in b or b in a)
    return {
        "channel_id": item["id"],
        "channel_name": title,
        "handle": item["snippet"].get("customUrl"),
        "uploads_playlist_id": item["contentDetails"]["relatedPlaylists"]["uploads"],
        "reported_video_count": int(item.get("statistics", {}).get("videoCount", 0)),
        "name_matches_expected": name_matches,
    }
