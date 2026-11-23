"""Shared step: video IDs -> full video metadata (50 IDs per API call)."""
from .youtube_client import YouTubeClient


def fetch_video_details(client: YouTubeClient, video_ids: list[str]) -> dict[str, dict]:
    """Return {video_id: api_item}. IDs missing from the result were deleted, private or blocked."""
    found: dict[str, dict] = {}
    for start in range(0, len(video_ids), 50):
        batch = video_ids[start : start + 50]
        for item in client.videos(batch).get("items", []):
            found[item["id"]] = item
    return found
