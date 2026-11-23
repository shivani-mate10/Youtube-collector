import json

import pytest

from collector.models import CollectionCategory
from collector.pipeline import collect_channels
from collector.quota import QuotaExceeded, QuotaTracker
from collector.resolvers.channel import parse_channel_url
from collector.storage import Storage

CH_ID = "UC" + "a" * 22


def test_parse_channel_url_ignores_tracking_params():
    assert parse_channel_url("https://youtube.com/@kittycoast?si=_cB9qu4nsZIBpnd5") == ("handle", "@kittycoast")
    assert parse_channel_url(f"https://www.youtube.com/channel/{CH_ID}") == ("id", CH_ID)
    with pytest.raises(ValueError):
        parse_channel_url("https://youtube.com/watch?v=abc")


class FakeClient:
    """Stands in for YouTubeClient: 4 uploads, one private, one deleted from the API."""

    def __init__(self, quota):
        self.quota = quota
        self.on_response = None

    def channel_by_handle(self, handle):
        self.quota.charge(1)
        return {"id": CH_ID, "snippet": {"title": "Kitty Coast", "customUrl": "@kittycoast"},
                "contentDetails": {"relatedPlaylists": {"uploads": "UU" + "a" * 22}},
                "statistics": {"videoCount": "4"}}

    channel_by_id = channel_by_handle

    def playlist_page(self, playlist_id, token=None):
        self.quota.charge(1)
        def item(vid, title, privacy="public"):
            return {"snippet": {"title": title}, "contentDetails": {"videoId": vid}, "status": {"privacyStatus": privacy}}
        return {"items": [item("v1", "One"), item("v2", "Private video", "private"),
                          item("v3", "Three"), item("v4", "Four")]}

    def videos(self, ids):
        self.quota.charge(1)
        def vid(i, **stats):
            return {"id": i, "snippet": {"title": i, "channelId": CH_ID, "channelTitle": "Kitty Coast",
                                          "publishedAt": "2026-01-01T00:00:00Z",
                                          "thumbnails": {"default": {"url": "http://t/d"}, "high": {"url": "http://t/h"}}},
                    "statistics": stats, "contentDetails": {"duration": "PT1M5S"}, "status": {}}
        # v4 is not returned: deleted/blocked
        return {"items": [vid("v1", viewCount="10", likeCount="2", commentCount="1"), vid("v3", viewCount="5")]}


@pytest.fixture
def env(tmp_path):
    quota = QuotaTracker(tmp_path / "data" / "state" / "quota.json")
    return quota, Storage(tmp_path / "data"), tmp_path


def run(env, category=CollectionCategory.INAPPROPRIATE, client=None):
    quota, storage, _ = env
    channels = [{"name": "Kitty Coast", "url": "https://youtube.com/@kittycoast?si=x", "channel_group": "g"}]
    return collect_channels(category, channels, client or FakeClient(quota), storage, 30, "run1")


def test_collect_stores_available_and_tracks_unavailable(env):
    summary = run(env)
    assert summary["channels"][0]["collected"] == 2
    assert summary["channels"][0]["unavailable"] == 2
    base = env[2] / "data" / "inappropriate"
    recs = [json.loads(l) for l in (base / "videos.jsonl").read_text().splitlines()]
    assert {r["video_id"] for r in recs} == {"v1", "v3"}
    first = next(r for r in recs if r["video_id"] == "v1")
    assert first["collection_category"] == "INAPPROPRIATE"
    assert first["annotation_status"] == "PENDING"
    assert first["thumbnail_url"] == "http://t/h"
    assert first["duration_seconds"] == 65
    assert first["like_count"] == 2
    third = next(r for r in recs if r["video_id"] == "v3")
    assert third["like_count"] is None and third["comment_count"] is None
    unavailable = {json.loads(l)["video_id"] for l in (base / "unavailable.jsonl").read_text().splitlines()}
    assert unavailable == {"v2", "v4"}
    assert (base / "videos.csv").read_text().startswith("video_id,video_url,title")


def test_rerun_and_other_category_do_not_duplicate(env):
    run(env)
    run(env)  # same category again
    run(env, CollectionCategory.NORMAL_KIDS_VIDEO)  # same videos found by another category
    base = env[2] / "data"
    recs = [json.loads(l) for l in (base / "inappropriate" / "videos.jsonl").read_text().splitlines()]
    assert len(recs) == 2
    assert recs[0]["source_categories"] == ["INAPPROPRIATE", "NORMAL_KIDS_VIDEO"]
    assert recs[0]["collection_category"] == "INAPPROPRIATE"
    assert not (base / "normal" / "videos.jsonl").exists() or (base / "normal" / "videos.jsonl").read_text() == ""


def test_quota_guard_stops_run(env):
    quota, storage, _ = env
    tight = QuotaTracker(quota.path, daily_limit=10000, safety_margin=9999)  # only 1 unit allowed
    summary = run((tight, storage, env[2]), client=FakeClient(tight))
    assert summary["stopped_by_quota"] is True
    with pytest.raises(QuotaExceeded):
        tight.charge(5)


def test_popular_regions_dedupe_and_sources(env):
    from collector.pipeline import collect_popular

    quota, storage, tmp = env

    class PopularClient:
        on_response = None

        def __init__(self):
            self.quota = quota

        def most_popular(self, region, token=None):
            self.quota.charge(1)
            def vid(i):
                return {"id": i, "snippet": {"title": i, "channelId": "c", "channelTitle": "C", "publishedAt": "2026-01-01T00:00:00Z",
                                              "thumbnails": {}}, "statistics": {"viewCount": "1"}, "contentDetails": {"duration": "PT5S"}, "status": {}}
            # "shared" charts in both regions; page has 3 items, no next page
            return {"items": [vid("shared"), vid(f"only{region}"), vid(f"extra{region}")]}

    summary = collect_popular({"IN": 2, "US": 3}, PopularClient(), storage, "run2")
    assert [r["collected"] for r in summary["channels"]] == [2, 3]
    recs = [json.loads(l) for l in (tmp / "data" / "popular" / "videos.jsonl").read_text().splitlines()]
    assert len(recs) == 5 - 1  # "shared" appears once
    shared = next(r for r in recs if r["video_id"] == "shared")
    assert [s["source_id"] for s in shared["sources"]] == ["IN", "US"]
    assert shared["collection_category"] == "POPULAR_VIDEO"


def test_random_sampler_skips_known_ids_and_stops_at_target(env):
    import random
    from datetime import date
    from collector.pipeline import collect_random
    from collector.sources.random_search import random_window

    quota, storage, tmp = env
    after, before = random_window(date(2026, 1, 1), 2, random.Random(1), today=date(2026, 2, 1))
    assert after < before and after >= "2026-01-01" and before <= "2026-02-01T00:00:00Z"

    # "known1" already stored under another category -> must be skipped.
    run(env)  # stores v1, v3 under INAPPROPRIATE

    class SearchClient:
        on_response = None
        calls = 0

        def __init__(self):
            self.quota = quota

        def search_videos(self, q, a, b):
            self.calls += 1
            return {"items": [{"id": {"videoId": i}} for i in ("v1", "v3", f"r{self.calls}a", f"r{self.calls}b", f"r{self.calls}c")]}

        def videos(self, ids):
            def vid(i):
                return {"id": i, "snippet": {"title": i, "channelId": "c", "channelTitle": "C",
                                              "publishedAt": "2026-01-02T00:00:00Z", "thumbnails": {}},
                        "statistics": {}, "contentDetails": {"duration": "PT9S"}, "status": {}}
            return {"items": [vid(i) for i in ids if not i.endswith("c")]}  # *c are "unavailable"

    client = SearchClient()
    summary = collect_random(client, storage, "run3", ["apple"], random.Random(7), target=5,
                             picks_per_search=3, max_searches=20, window_start=date(2026, 1, 1), window_days=2)
    recs = [json.loads(l) for l in (tmp / "data" / "random" / "videos.jsonl").read_text().splitlines()]
    assert len(recs) == 5 and {r["video_id"] for r in recs}.isdisjoint({"v1", "v3"})
    assert all(r["collection_category"] == "RANDOM_VIDEO" and r["sources"][0]["source_type"] == "random_search" for r in recs)
    assert summary["channels"][0]["collected"] == 5
    # Top-up: a second run with the same target makes no new searches.
    before_calls = client.calls
    collect_random(client, storage, "run4", ["apple"], random.Random(8), target=5, picks_per_search=3,
                   max_searches=20, window_start=date(2026, 1, 1), window_days=2)
    assert client.calls == before_calls


def test_keyword_search_resumes_and_merges(env):
    from collector.pipeline import collect_keywords

    quota, storage, tmp = env
    run(env)  # stores v1, v3 as INAPPROPRIATE

    class KwClient:
        on_response = None
        searched = []

        def __init__(self):
            self.quota = quota

        def search_keyword(self, q, n):
            self.searched.append(q)
            if len(self.searched) == 2:
                raise QuotaExceeded("stop")
            return {"items": [{"id": {"videoId": i}} for i in ("v1", f"{q}-a", f"{q}-b")]}

        def videos(self, ids):
            def vid(i):
                return {"id": i, "snippet": {"title": i, "channelId": "c", "channelTitle": "C",
                                              "publishedAt": "2026-01-02T00:00:00Z", "thumbnails": {}},
                        "statistics": {}, "contentDetails": {"duration": "PT9S"}, "status": {}}
            return {"items": [vid(i) for i in ids]}

    sections = {"S": ["k1", "k2", "k3"]}
    client = KwClient()
    cat = CollectionCategory.NORMAL_KIDS_VIDEO
    s1 = collect_keywords(cat, sections, client, storage, "r5", per_keyword=2, extra_source={"target_age_group": "5-12"})
    assert s1["stopped_by_quota"] and s1["channels"][0]["keywords_done"] == 1
    assert storage.done_keywords(cat) == {"k1"}
    s2 = collect_keywords(cat, sections, client, storage, "r6", per_keyword=2)
    assert client.searched == ["k1", "k2", "k2", "k3"]  # k1 not repeated after resume
    assert storage.done_keywords(cat) == {"k1", "k2", "k3"}
    normal = [json.loads(l) for l in (tmp / "data" / "normal" / "videos.jsonl").read_text().splitlines()]
    assert {r["video_id"] for r in normal} == {"k1-a", "k2-a", "k3-a"}  # per_keyword=2 -> "v1" + first new
    merged = [json.loads(l) for l in (tmp / "data" / "inappropriate" / "videos.jsonl").read_text().splitlines()]
    v1 = next(r for r in merged if r["video_id"] == "v1")
    assert v1["source_categories"] == ["INAPPROPRIATE", "NORMAL_KIDS_VIDEO"] and v1["collection_category"] == "INAPPROPRIATE"
    assert any(s.get("target_age_group") == "5-12" for s in v1["sources"])
