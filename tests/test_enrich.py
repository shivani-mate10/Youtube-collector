import json

from collector.enrich import pipeline


def test_pick_evenly_spreads_and_skips_first_frame():
    items = list(range(100))
    picked = pipeline.pick_evenly(items, 10)
    assert len(picked) == 10 and picked[0] == 5 and picked[-1] == 95
    assert picked == sorted(set(picked))


def test_pick_evenly_short_list_keeps_all():
    assert pipeline.pick_evenly([1, 2, 3], 16) == [1, 2, 3]


def test_prune_frames_keeps_n_and_deletes_rest(tmp_path):
    (tmp_path / "previews").mkdir()
    index = []
    for i in range(40):
        name = f"previews/frame_{i:06d}_{i * 1000:09d}ms.jpg"
        (tmp_path / name).write_bytes(b"x")
        index.append({"image": name})
    (tmp_path / "previews" / "index.json").write_text(json.dumps(index))
    kept, available = pipeline.prune_frames(tmp_path, 8)
    assert (kept, available) == (8, 40)
    assert len(list((tmp_path / "frames").glob("*.jpg"))) == 8
    pipeline._clean_up(tmp_path)
    assert not (tmp_path / "previews").exists()
    assert len(list((tmp_path / "frames").glob("*.jpg"))) == 8


def test_labelled_ids_only_labelled(tmp_path):
    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "dataset.csv").write_text("video_id,final_label,binary_label\na,Suitable,Appropriate\nb,,\nc,Restricted,Inappropriate\nd,Cannot review,\n")
    assert pipeline.labelled_ids(tmp_path) == ["a", "c"]


def save_ok(out_dir, vid):
    """What the real enrich_one leaves behind for a finished video."""
    row = {"video_id": vid, "status": "ok", "frames_kept": 16, "frames_available": 40}
    (out_dir / vid).mkdir(parents=True, exist_ok=True)
    (out_dir / vid / "enrich.json").write_text(json.dumps(row))
    return row


def test_run_waits_and_continues_after_throttle(tmp_path, monkeypatch):
    from collector.enrich import scraper

    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "dataset.csv").write_text(
        "video_id,final_label,binary_label\na,x,Appropriate\nb,x,Appropriate\nc,x,Inappropriate\n")

    class FakeYdl:
        def __init__(self, policy, proxy=None): self.policy = policy
        def __enter__(self): return self
        def __exit__(self, *a): return False

    calls, waits = [], []

    def fake_enrich(ydl, vid, out_dir, keep, skip_captions=False):
        calls.append(vid)
        if calls.count("b") == 1 and vid == "b":          # first attempt at b is throttled
            raise scraper.BatchStopped("throttled")
        return {"video_id": vid, "status": "ok", "frames_kept": 16, "frames_available": 40}

    monkeypatch.setattr(scraper, "PacedYoutubeDL", FakeYdl)
    monkeypatch.setattr(pipeline, "enrich_one", fake_enrich)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    result = pipeline.run(tmp_path, log=lambda *_: None, sleep=waits.append, cooldown_minutes=10)
    assert calls == ["a", "b", "b", "c"]                   # b retried, nothing skipped
    assert waits == [600] and result["throttles"] == 1 and result["counts"] == {"ok": 3} and not result["stopped"]
    assert not (tmp_path / "enrichment" / ".scraper.lock").exists()


def test_run_can_still_stop_on_throttle(tmp_path, monkeypatch):
    from collector.enrich import scraper

    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "dataset.csv").write_text("video_id,final_label,binary_label\na,x,Appropriate\n")

    class FakeYdl:
        def __init__(self, policy, proxy=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def always_throttled(*a, **kw):
        raise scraper.BatchStopped("throttled")

    monkeypatch.setattr(scraper, "PacedYoutubeDL", FakeYdl)
    monkeypatch.setattr(pipeline, "enrich_one", always_throttled)
    result = pipeline.run(tmp_path, log=lambda *_: None, auto_retry=False)
    assert result["stopped"] and result["remaining"] == 1


def test_video_that_is_always_throttled_is_set_aside_not_looped(tmp_path, monkeypatch):
    from collector.enrich import scraper

    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "dataset.csv").write_text(
        "video_id,final_label,binary_label\nbad,x,Appropriate\ngood,x,Appropriate\n")

    class FakeYdl:
        def __init__(self, policy, proxy=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    calls, waits = [], []

    def fake_enrich(ydl, vid, out_dir, keep, skip_captions=False):
        calls.append(vid)
        if vid == "bad":
            raise scraper.BatchStopped("throttled")
        return save_ok(out_dir, vid)

    monkeypatch.setattr(scraper, "PacedYoutubeDL", FakeYdl)
    monkeypatch.setattr(pipeline, "enrich_one", fake_enrich)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    result = pipeline.run(tmp_path, log=lambda *_: None, sleep=waits.append, cooldown_minutes=30)
    assert calls == ["bad", "bad", "bad", "good"]               # full, full, frames-only, then moved on
    assert result["counts"] == {"throttled_skip": 1, "ok": 1} and result["remaining"] == 0
    assert waits == [1800, 120, 120]                             # long wait, pause before frames-only, pause after set-aside
    assert pipeline.read_done(tmp_path / "enrichment")["bad"]["status"] == "throttled_skip"
    # next run: the set-aside video is retried, after any new ones
    (tmp_path / "final" / "dataset.csv").write_text(
        "video_id,final_label,binary_label\nbad,x,Appropriate\ngood,x,Appropriate\nnew,x,Appropriate\n")
    calls.clear()
    monkeypatch.setattr(pipeline, "enrich_one", lambda ydl, vid, o, k, skip_captions=False: (calls.append(vid), save_ok(o, vid))[1])
    pipeline.run(tmp_path, log=lambda *_: None, sleep=waits.append)
    assert calls == ["new", "bad"]


def test_throttled_captions_fall_back_to_frames_only(tmp_path, monkeypatch):
    from collector.enrich import scraper

    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "dataset.csv").write_text("video_id,final_label,binary_label\nv,x,Appropriate\n")

    class FakeYdl:
        def __init__(self, policy, proxy=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = []

    def fake_enrich(ydl, vid, out_dir, keep, skip_captions=False):
        seen.append(skip_captions)
        if not skip_captions:
            raise scraper.BatchStopped("captions throttled")
        row = {"video_id": vid, "status": "captions_throttled", "frames_kept": 16, "frames_available": 40}
        (out_dir / vid).mkdir(parents=True, exist_ok=True)
        (out_dir / vid / "enrich.json").write_text(json.dumps(row))
        return row

    monkeypatch.setattr(scraper, "PacedYoutubeDL", FakeYdl)
    monkeypatch.setattr(pipeline, "enrich_one", fake_enrich)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    result = pipeline.run(tmp_path, log=lambda *_: None, sleep=lambda s: None)
    assert seen == [False, False, True] and result["counts"] == {"captions_throttled": 1}
    assert "captions_throttled" in pipeline.SET_ASIDE


def test_no_captions_mode_skips_captions_for_every_video(tmp_path, monkeypatch):
    from collector.enrich import scraper

    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "dataset.csv").write_text("video_id,final_label,binary_label\na,x,Appropriate\nb,x,Appropriate\n")

    class FakeYdl:
        def __init__(self, policy, proxy=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = []

    def fake_enrich(ydl, vid, out_dir, keep, skip_captions=False):
        seen.append(skip_captions)
        return save_ok(out_dir, vid)

    monkeypatch.setattr(scraper, "PacedYoutubeDL", FakeYdl)
    monkeypatch.setattr(pipeline, "enrich_one", fake_enrich)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    pipeline.run(tmp_path, log=lambda *_: None, sleep=lambda s: None, skip_captions=True)
    assert seen == [True, True]


def test_captions_only_touches_only_captions_throttled_videos(tmp_path, monkeypatch):
    from collector.enrich import scraper

    (tmp_path / "final").mkdir()
    (tmp_path / "final" / "dataset.csv").write_text(
        "video_id,final_label,binary_label\nok1,x,Appropriate\nneed,x,Appropriate\nnew,x,Appropriate\n")
    out = tmp_path / "enrichment"
    for vid, status in (("ok1", "ok"), ("need", "captions_throttled")):
        (out / vid / "frames").mkdir(parents=True)
        (out / vid / "frames" / "f.jpg").write_bytes(b"x")
        (out / vid / "enrich.json").write_text(json.dumps(
            {"video_id": vid, "status": status, "frames_kept": 16, "frames_available": 40, "has_captions": status == "ok"}))

    class FakeYdl:
        def __init__(self, policy, proxy=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = []

    def fake_process(ydl, vid, output, refresh=False, resume=False, skip=()):
        seen.append((vid, skip))
        folder = output / vid
        folder.mkdir(parents=True)
        (folder / "subtitles.json").write_text("{}")
        (folder / "transcript.txt").write_text("hello")
        (folder / "manifest.json").write_text(json.dumps({"captions": {"status": "complete", "language": "en",
                                                                         "source": "automatic_captions", "segments": 5}}))
        return {"id": vid, "status": "incomplete"}

    monkeypatch.setattr(scraper, "PacedYoutubeDL", FakeYdl)
    monkeypatch.setattr(scraper, "process_video", fake_process)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    result = pipeline.run(tmp_path, log=lambda *_: None, sleep=lambda s: None, captions_only=True)
    assert seen == [("need", ("previews",))]                      # only that video, frames not fetched
    row = json.loads((out / "need" / "enrich.json").read_text())
    assert row["status"] == "ok" and row["has_captions"] and row["frames_kept"] == 16
    assert (out / "need" / "transcript.txt").read_text() == "hello" and (out / "need" / "frames" / "f.jpg").exists()
    assert not (out / ".captions_tmp").exists() and result["counts"] == {"ok": 1}
