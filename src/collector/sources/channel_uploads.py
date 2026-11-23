"""Most recent videos of a channel, read from its uploads playlist (newest first)."""
from typing import Iterator

from ..youtube_client import YouTubeClient
from .base import Candidate, VideoSource

_PLACEHOLDER_TITLES = {"private video": "private", "deleted video": "deleted"}


class ChannelUploadsSource(VideoSource):
    def __init__(self, client: YouTubeClient, channel: dict, source_name: str, limit: int):
        self.client = client
        self.channel = channel
        self.source_name = source_name
        self.limit = limit

    def candidates(self) -> Iterator[Candidate]:
        playlist_id = self.channel["uploads_playlist_id"]
        wanted, token = 0, None
        while wanted < self.limit:
            page = self.client.playlist_page(playlist_id, token)
            for item in page.get("items", []):
                title = item["snippet"].get("title", "").strip().casefold()
                privacy = item.get("status", {}).get("privacyStatus")
                reason = _PLACEHOLDER_TITLES.get(title) or ("private" if privacy == "private" else None)
                video_id = item["contentDetails"]["videoId"]
                yield Candidate(
                    video_id=video_id,
                    source_type="channel_uploads",
                    source_id=self.channel["channel_id"],
                    source_name=self.source_name,
                    unavailable_reason=reason,
                )
                if reason is None:
                    wanted += 1
                    if wanted >= self.limit:
                        return
            token = page.get("nextPageToken")
            if not token:
                return
