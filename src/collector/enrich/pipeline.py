"""Captions + a few frames for labelled videos, stored under data/enrichment/<video_id>/.

Uses the scraper (yt-dlp, no API quota) one video at a time, then keeps only N evenly spaced
frames and deletes the other previews and storyboard sheets.

Per video folder:  metadata.json, subtitles.json, transcript.txt, frames/*.jpg, enrich.json
Summary table:     data/enrichment/index.csv  (one row per video, join on video_id)
"""
import csv
import json
import os
import random
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

INDEX_FIELDS = ["video_id", "status", "frames_kept", "frames_available", "has_captions", "caption_language",
                "caption_source", "caption_translated", "caption_segments", "error", "enriched_at"]
SET_ASIDE = {"throttled_skip", "captions_throttled"}  # retried on the next run, after all other videos
RETRY_STATUSES = {"failed", "interrupted"} | SET_ASIDE   # everything else is final and skipped next time
MAX_THROTTLES_PER_VIDEO = 3   # 1st: wait + retry. 2nd: retry WITHOUT captions (frames only). 3rd: set aside


def labelled_ids(data_dir: Path) -> list[str]:
    dataset = data_dir / "final" / "dataset.csv"
    if not dataset.exists():
        raise FileNotFoundError("data/final/dataset.csv not found. Run `collector label merge` first.")
    with dataset.open(newline="", encoding="utf-8") as fh:
        return [r["video_id"] for r in csv.DictReader(fh) if (r.get("binary_label") or "").strip()]  # labelled, not "Cannot review"


def pick_evenly(items: list, n: int) -> list:
    """n items spread over the list, one from the middle of each of n equal parts.

    Middle of each part (not the very first frame) avoids black intro frames. Fewer than n -> all."""
    if len(items) <= n:
        return list(items)
    return [items[int((i + 0.5) * len(items) / n)] for i in range(n)]


def prune_frames(root: Path, keep: int) -> tuple[int, int]:
    """Move `keep` evenly spaced previews to frames/, delete the rest. Returns (kept, available)."""
    index_path = root / "previews" / "index.json"
    if not index_path.exists():
        return 0, 0
    index = json.loads(index_path.read_text())
    chosen = pick_evenly(index, keep)
    frames = root / "frames"
    frames.mkdir(exist_ok=True)
    for entry in chosen:
        src = root / entry["image"]
        if src.exists():
            shutil.move(str(src), str(frames / src.name))
    return len(chosen), len(index)


def _clean_up(root: Path) -> None:
    for name in ("previews", "storyboards"):
        shutil.rmtree(root / name, ignore_errors=True)
    for name in ("manifest.json", "subtitles.raw.json"):
        (root / name).unlink(missing_ok=True)


def enrich_one(ydl, video_id: str, out_dir: Path, keep_frames: int, skip_captions: bool = False) -> dict:
    from . import scraper
    root = out_dir / video_id
    if root.exists():
        shutil.rmtree(root)  # retry from scratch
    result = scraper.process_video(ydl, video_id, out_dir, skip=("captions",) if skip_captions else ())
    manifest = {}
    try:
        manifest = json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError):
        pass
    kept, available = prune_frames(root, keep_frames)
    _clean_up(root)

    captions = manifest.get("captions") or {}
    has_captions = captions.get("status") == "complete"
    if result["status"] == "failed":
        status = "failed"
    elif skip_captions and kept > 0:
        status = "captions_throttled"            # frames saved; captions to be fetched on a later run
    elif kept == 0 and not has_captions:
        status = "nothing_found"
    elif kept == 0:
        status = "no_frames"
    elif not has_captions:
        status = "no_captions"
    else:
        status = "ok"
    row = {
        "video_id": video_id, "status": status, "frames_kept": kept, "frames_available": available,
        "has_captions": has_captions, "caption_language": captions.get("language", ""),
        "caption_source": captions.get("source", ""), "caption_translated": captions.get("translated", ""),
        "caption_segments": captions.get("segments", ""),
        "error": manifest.get("error") or (manifest.get("previews") or {}).get("error")
                 or (captions.get("reason") if not has_captions else "") or "",
        "enriched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (root / "enrich.json").write_text(json.dumps(row, indent=1))
    return row


def add_captions(ydl, video_id: str, out_dir: Path) -> dict:
    """Fetch ONLY the captions for a video whose frames were already saved. Frames are left untouched.

    Raises BatchStopped when YouTube throttles (the caller waits / gives up on this video)."""
    from . import scraper
    root, tmp = out_dir / video_id, out_dir / ".captions_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        scraper.process_video(ydl, video_id, tmp, skip=("previews",))
        manifest = {}
        try:
            manifest = json.loads((tmp / video_id / "manifest.json").read_text())
        except (OSError, ValueError):
            pass
        captions = manifest.get("captions") or {}
        has_captions = captions.get("status") == "complete"
        if has_captions:
            for name in ("subtitles.json", "transcript.txt"):
                shutil.copy(tmp / video_id / name, root / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    row = json.loads((root / "enrich.json").read_text())
    if has_captions:
        row.update(status="ok" if row.get("frames_kept") else "no_frames", has_captions=True,
                   caption_language=captions.get("language", ""), caption_source=captions.get("source", ""),
                   caption_translated=captions.get("translated", ""), caption_segments=captions.get("segments", ""),
                   error="")
    elif captions.get("status") == "missing":           # the video really has no English captions
        row.update(status="no_captions", has_captions=False, error=captions.get("reason", ""))
    else:                                               # some other failure: leave as is, try again next time
        row["error"] = captions.get("error") or manifest.get("error") or "captions could not be fetched"
    row["enriched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (root / "enrich.json").write_text(json.dumps(row, indent=1))
    return row


def read_done(out_dir: Path) -> dict[str, dict]:
    done = {}
    for path in out_dir.glob("*/enrich.json"):
        try:
            row = json.loads(path.read_text())
            done[row["video_id"]] = row
        except (OSError, ValueError, KeyError):
            continue
    return done


def write_index(out_dir: Path) -> None:
    rows = sorted(read_done(out_dir).values(), key=lambda r: r["video_id"])
    with (out_dir / "index.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run(data_dir: Path, keep_frames: int = 16, max_videos: int | None = None, only_ids: list[str] | None = None,
        request_delay=(1, 2), video_delay=(5, 10), proxy: str | None = None, log=print,
        auto_retry: bool = True, cooldown_minutes: float = 30, max_cooldown_minutes: float = 240,
        sleep=time.sleep, skip_captions: bool = False, captions_only: bool = False) -> dict:
    """Enrich labelled videos one at a time.

    When YouTube throttles (the scraper stops itself), auto_retry waits `cooldown_minutes`, then carries on with the
    same video using slower pacing. Each further throttle doubles the wait (up to max_cooldown_minutes).
    Ctrl+C always stops. auto_retry=False stops at the first throttle instead."""
    from . import scraper
    out_dir = data_dir / "enrichment"
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = only_ids or labelled_ids(data_dir)
    done = read_done(out_dir)
    if captions_only:     # only videos whose frames are saved but whose captions are still missing
        todo = [v for v in dict.fromkeys(ids) if done.get(v, {}).get("status") == "captions_throttled"]
    else:
        todo = [v for v in dict.fromkeys(ids) if v not in done or done[v]["status"] in RETRY_STATUSES]
    todo.sort(key=lambda v: v in done and done[v]["status"] in SET_ASIDE)   # set-aside videos go last
    skipped = len(set(ids)) - len(todo)
    if max_videos:
        todo = todo[:max_videos]

    lock = out_dir / ".scraper.lock"
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        raise RuntimeError(f"{lock} exists; another run may be active. Remove it only if no run is going.")

    counts: dict[str, int] = {}
    stopped, throttles, wait = False, 0, cooldown_minutes
    slow = 1.0                                   # pacing multiplier, grows after each throttle
    position = 0
    strikes: dict[str, int] = {}                 # throttles in a row on the same video
    frames_only: set[str] = set()                # videos retried without captions after a throttle
    try:
        while position < len(todo):
            policy = scraper.NetworkPolicy(request_delay[0] * slow, request_delay[1] * slow)
            try:
                with scraper.PacedYoutubeDL(policy, proxy) as ydl:
                    while position < len(todo):
                        video_id = todo[position]
                        if position:
                            time.sleep(random.uniform(video_delay[0] * slow, video_delay[1] * slow))
                        if captions_only:
                            row = add_captions(ydl, video_id, out_dir)
                        else:
                            row = enrich_one(ydl, video_id, out_dir, keep_frames,
                                             skip_captions=skip_captions or video_id in frames_only)
                        counts[row["status"]] = counts.get(row["status"], 0) + 1
                        position += 1
                        wait = cooldown_minutes   # progress again: next throttle starts with the short wait
                        log(f"[{position}/{len(todo)}] {video_id}  {row['status']}  frames {row['frames_kept']}/{row['frames_available']}")
            except scraper.BatchStopped as err:
                throttles += 1
                video_id = todo[position]
                strikes[video_id] = strikes.get(video_id, 0) + 1
                log(f"Throttled by YouTube on {video_id}: {scraper.safe_error(err)}")
                if not auto_retry:
                    stopped = True
                    break
                if captions_only and strikes[video_id] >= MAX_THROTTLES_PER_VIDEO - 1:
                    position += 1                        # leave it as it is; it stays captions_throttled
                    log(f"Captions for {video_id} still refused; left for a later run. Continuing in 2 min.")
                    sleep(120)
                    slow = min(slow * 1.5, 4.0)
                    continue
                if strikes[video_id] == MAX_THROTTLES_PER_VIDEO - 1:
                    # Caption downloads are what YouTube refuses most often. Take the frames now, captions later.
                    frames_only.add(video_id)
                    log(f"Retrying {video_id} without captions (frames only; captions on a later run). Continuing in 2 min.")
                    sleep(120)
                    slow = min(slow * 1.5, 4.0)
                    continue
                if strikes[video_id] >= MAX_THROTTLES_PER_VIDEO:
                    # Refused even without captions. Don't loop on it.
                    shutil.rmtree(out_dir / video_id, ignore_errors=True)
                    (out_dir / video_id).mkdir()
                    row = {"video_id": video_id, "status": "throttled_skip", "frames_kept": 0, "frames_available": 0,
                           "has_captions": False, "error": f"YouTube throttled this video {strikes[video_id]} times; set aside",
                           "enriched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                    (out_dir / video_id / "enrich.json").write_text(json.dumps(row, indent=1))
                    counts["throttled_skip"] = counts.get("throttled_skip", 0) + 1
                    position += 1
                    log(f"Set {video_id} aside (retried on the next run, after the others). Continuing in 2 min.")
                    sleep(120)
                    slow = min(slow * 1.5, 4.0)
                    continue
                resume_at = datetime.now() + timedelta(minutes=wait)
                log(f"Waiting {wait:g} min, will resume at {resume_at:%H:%M} (Ctrl+C to stop). "
                    f"{len(todo) - position} videos left.")
                write_index(out_dir)
                sleep(wait * 60)
                wait = min(wait * 2, max_cooldown_minutes)
                slow = min(slow * 1.5, 4.0)
    except KeyboardInterrupt:
        stopped = True
        log("Stopped by you. Run the same command again to continue.")
    finally:
        lock.unlink(missing_ok=True)
        write_index(out_dir)
    return {"processed": sum(counts.values()), "skipped": skipped, "counts": counts, "stopped": stopped,
            "throttles": throttles, "remaining": len(todo) - sum(counts.values())}
