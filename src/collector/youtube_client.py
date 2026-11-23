"""The only module that talks to the YouTube Data API."""
import json
import time

import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .quota import (
    COST_CHANNELS_LIST,
    COST_PLAYLIST_ITEMS_LIST,
    COST_SEARCH_LIST,
    COST_VIDEOS_LIST,
    QuotaExceeded,
    QuotaTracker,
)

MAX_ATTEMPTS = 5
TRANSIENT_STATUS = {500, 502, 503, 504}
TRANSIENT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}

VIDEO_PARTS = "snippet,statistics,contentDetails,status"


class YouTubeError(Exception):
    pass


class NotFound(YouTubeError):
    pass


def _reason(err: HttpError) -> str:
    try:
        return json.loads(err.content)["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


class YouTubeClient:
    def __init__(self, api_key: str, quota: QuotaTracker, on_response=None, sleep=time.sleep):
        self._yt = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
        self.quota = quota
        # Called as on_response(name, params, response) after every successful call.
        self.on_response = on_response
        self._sleep = sleep

    def _call(self, name: str, request, cost: int, params: dict) -> dict:
        self.quota.charge(cost)
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = request.execute()
            except HttpError as err:
                status, reason = err.resp.status, _reason(err)
                if status == 403 and reason in QUOTA_REASONS:
                    raise QuotaExceeded(f"YouTube reported {reason}") from err
                if status == 404:
                    raise NotFound(f"{name}: not found ({params})") from err
                transient = status in TRANSIENT_STATUS or reason in TRANSIENT_REASONS
                if not transient or attempt == MAX_ATTEMPTS - 1:
                    raise YouTubeError(f"{name} failed: HTTP {status} {reason}") from err
            except (OSError, httplib2.HttpLib2Error) as err:
                if attempt == MAX_ATTEMPTS - 1:
                    raise YouTubeError(f"{name} failed: {err}") from err
            else:
                if self.on_response:
                    self.on_response(name, params, response)
                return response
            self._sleep(2**attempt)
        raise YouTubeError(f"{name} failed")  # pragma: no cover

    def channel_by_handle(self, handle: str) -> dict | None:
        params = {"part": "snippet,contentDetails,statistics", "forHandle": handle}
        resp = self._call("channels", self._yt.channels().list(**params), COST_CHANNELS_LIST, params)
        items = resp.get("items", [])
        return items[0] if items else None

    def channel_by_id(self, channel_id: str) -> dict | None:
        params = {"part": "snippet,contentDetails,statistics", "id": channel_id}
        resp = self._call("channels", self._yt.channels().list(**params), COST_CHANNELS_LIST, params)
        items = resp.get("items", [])
        return items[0] if items else None

    def playlist_page(self, playlist_id: str, page_token: str | None = None) -> dict:
        params = {
            "part": "snippet,contentDetails,status",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token
        return self._call(
            "playlistItems",
            self._yt.playlistItems().list(**params),
            COST_PLAYLIST_ITEMS_LIST,
            params,
        )

    def most_popular(self, region_code: str, page_token: str | None = None) -> dict:
        """One page (up to 50) of the mostPopular chart; returns full video items."""
        params = {"part": VIDEO_PARTS, "chart": "mostPopular", "regionCode": region_code, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        return self._call("mostPopular", self._yt.videos().list(**params), COST_VIDEOS_LIST, params)

    def search_videos(self, query: str, published_after: str, published_before: str) -> dict:
        """Costs 100 units. Returns up to 50 video IDs inside the given publish window."""
        params = {
            "part": "id",
            "q": query,
            "type": "video",
            # Newest-first inside a short window avoids relevance ranking's bias toward popular videos.
            "order": "date",
            "publishedAfter": published_after,
            "publishedBefore": published_before,
            "maxResults": 50,
        }
        return self._call("search", self._yt.search().list(**params), COST_SEARCH_LIST, params)

    def search_keyword(self, query: str, max_results: int) -> dict:
        """Costs 100 units however many results are asked for. Relevance-ranked video IDs."""
        params = {"part": "id", "q": query, "type": "video", "maxResults": min(max_results, 50)}
        return self._call("search", self._yt.search().list(**params), COST_SEARCH_LIST, params)

    def videos(self, video_ids: list[str]) -> dict:
        params = {"part": VIDEO_PARTS, "id": ",".join(video_ids), "maxResults": 50}
        return self._call("videos", self._yt.videos().list(**params), COST_VIDEOS_LIST, params)
