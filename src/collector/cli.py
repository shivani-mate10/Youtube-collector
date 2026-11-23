import argparse
import json
import os
import random
import sys
import time
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import labeling, thumbnails
from .models import CollectionCategory
from .pipeline import collect_channels, collect_keywords, collect_popular, collect_random, new_run_id
from .sources.random_search import load_words
from .quota import QuotaTracker
from .storage import Storage
from .youtube_client import YouTubeClient

ROOT = Path(__file__).resolve().parents[2]


def _settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())


def _quota(settings: dict) -> QuotaTracker:
    return QuotaTracker(ROOT / "data" / "state" / "quota.json", settings["daily_quota_limit"], settings["quota_safety_margin"])


def _run(collect_fn) -> int:
    """Shared wrapper: API key, quota, storage, run summary file and console report.

    collect_fn(client, storage, run_id, settings) -> summary dict, or None to abort.
    """
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key or api_key == "your_key_here":
        print("YOUTUBE_API_KEY is missing. Put it in .env (see .env.example).", file=sys.stderr)
        return 1

    settings = _settings()
    quota, storage = _quota(settings), Storage(ROOT / "data")
    client = YouTubeClient(api_key, quota)
    run_id = new_run_id()
    quota_before = quota.used()

    summary = collect_fn(client, storage, run_id, settings)
    if summary is None:
        return 1
    summary["quota_used_this_run"] = quota.used() - quota_before
    summary["quota_used_today"] = quota.used()

    runs = ROOT / "data" / "state" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps(summary, indent=1))

    for row in summary["channels"]:
        status = row["error"] or f"{row['collected']} videos, {row['unavailable']} unavailable"
        print(f"{row['name']:<20} {status}" + (f"  [!] {row['warning']}" if row.get("warning") else ""))
        if "keywords_remaining" in row:
            print(f"{'':<20} keywords done this run: {row['keywords_done']}, still to do: {row['keywords_remaining']}, "
                  f"videos kept: {row['collected']}")
    print(f"Quota used this run: {summary['quota_used_this_run']} (today: {summary['quota_used_today']})")
    if summary["stopped_by_quota"]:
        print("Stopped early because of the quota guard; re-run after midnight Pacific.")
    return 0


def _collect_channels(args, category: CollectionCategory, channels_file: str) -> int:
    def collect(client, storage, run_id, settings):
        channels = (yaml.safe_load((ROOT / "config" / "sources" / channels_file).read_text()) or {}).get("channels") or []
        if not channels:
            print(f"No channels listed in config/sources/{channels_file}.", file=sys.stderr)
            return None
        limit = args.max_per_channel or settings["max_videos_per_channel"]
        return collect_channels(category, channels, client, storage, limit, run_id)

    return _run(collect)


def cmd_collect_inappropriate(args) -> int:
    return _collect_channels(args, CollectionCategory.INAPPROPRIATE, "inappropriate_channels.yaml")


def cmd_collect_normal(args) -> int:
    return _collect_channels(args, CollectionCategory.NORMAL_KIDS_VIDEO, "normal_channels.yaml")


def cmd_collect_normal_keywords(args) -> int:
    def collect(client, storage, run_id, settings):
        cfg = yaml.safe_load((ROOT / "config" / "sources" / "normal_keywords.yaml").read_text())
        summary = collect_keywords(
            CollectionCategory.NORMAL_KIDS_VIDEO, cfg["sections"], client, storage, run_id,
            per_keyword=args.per_keyword or settings["keyword_search"]["per_keyword"],
            extra_source={"target_age_group": cfg.get("target_age_group")},
            max_keywords=args.max_keywords,
            target_total=args.target_total or settings["keyword_search"]["target_total"],
        )
        return summary

    return _run(collect)


def cmd_collect_random(args) -> int:
    def collect(client, storage, run_id, settings):
        cfg = settings["random"]
        words = load_words(ROOT / "config" / "sources" / "random_words.txt")
        seed = int(time.time())
        summary = collect_random(
            client, storage, run_id, words, random.Random(seed),
            target=args.target or cfg["target"],
            picks_per_search=cfg["picks_per_search"],
            max_searches=args.max_searches or cfg["max_searches"],
            window_start=date.fromisoformat(cfg["window_start"]),
            window_days=cfg["window_days"],
        )
        summary["seed"] = seed
        return summary

    return _run(collect)


def cmd_collect_popular(_args) -> int:
    def collect(client, storage, run_id, settings):
        regions = settings.get("popular_regions") or {}
        if not regions:
            print("No popular_regions in config/settings.yaml.", file=sys.stderr)
            return None
        return collect_popular(regions, client, storage, run_id)

    return _run(collect)


def _reviewers() -> list[str]:
    return yaml.safe_load((ROOT / "config" / "labeling.yaml").read_text())["reviewers"]


def cmd_label_export(args) -> int:
    try:
        result = labeling.export_tasks(ROOT / "data", _reviewers(), per_reviewer=args.per_reviewer, only=args.only)
    except ValueError as err:
        print(err, file=sys.stderr)
        return 1
    if not result["new"]:
        print("Nothing new to assign: every collected video is already assigned.")
        return 0
    print(f"Batch {result['batch']}: assigned {result['new']} new videos")
    for reviewer, path in result["files"].items():
        print(f"  {reviewer:<12} {result['per_reviewer'][reviewer]:>5} videos  ->  {path.relative_to(ROOT)}")
    categories = sorted({c for _, c in result["per_category"]})
    print(f"\n{'':<12}" + "".join(f"{c[:14]:>16}" for c in categories))
    for reviewer in result["files"]:
        print(f"{reviewer:<12}" + "".join(f"{result['per_category'][(reviewer, c)]:>16}" for c in categories))
    print("\nLabel Studio setup file: data/labels/label_studio_config.xml")
    return 0


def cmd_label_rebuild(_args) -> int:
    for name, count in labeling.rebuild_batches(ROOT / "data").items():
        print(f"{name:<28} {count} tasks")
    return 0


def cmd_label_merge(_args) -> int:
    try:
        result = labeling.merge_labels(ROOT / "data")
    except FileNotFoundError as err:
        print(err, file=sys.stderr)
        return 1
    print(f"Labelled {result['labelled']} of {result['total']} videos -> data/labels/labels.csv, data/final/dataset.csv")
    categories = sorted({c for c, _ in result["crosstab"]})
    labels = labeling.LABELS + [labeling.CANNOT_REVIEW]
    if categories:
        print(f"\n{'':<20}" + "".join(f"{lab:>14}" for lab in labels))
        for cat in categories:
            print(f"{cat:<20}" + "".join(f"{result['crosstab'][(cat, lab)]:>14}" for lab in labels))
    for warning in result["warnings"]:
        print("[!]", warning)
    return 0


def cmd_label_status(_args) -> int:
    for reviewer, (assigned, done) in labeling.status(ROOT / "data", _reviewers()).items():
        print(f"{reviewer:<12} assigned {assigned:>5}   labelled {done:>5}")
    return 0


def cmd_thumbnails(args) -> int:
    try:
        r = thumbnails.download_labelled(ROOT / "data", only_labelled=not args.all)
    except FileNotFoundError as err:
        print(err, file=sys.stderr)
        return 1
    print(f"Downloaded {r['downloaded']}, already had {r['skipped']}, failed {len(r['failed'])} -> data/thumbnails/")
    for vid, err in r["failed"]:
        print(f"  [!] {vid}: {err}")
    return 0


def _assigned_ids() -> list[str]:
    import csv
    with (ROOT / "data" / "labels" / "assignments.csv").open(newline="", encoding="utf-8") as fh:
        return [r["video_id"] for r in csv.DictReader(fh)]


def cmd_enrich(args) -> int:
    from .enrich import pipeline  # lazy: yt-dlp is only needed for this command
    try:
        r = pipeline.run(
            ROOT / "data", keep_frames=args.frames, max_videos=args.max, only_ids=_assigned_ids() if args.assigned else args.ids,
            request_delay=tuple(args.request_delay), video_delay=tuple(args.video_delay),
            proxy=os.environ.get("YOUTUBE_PROXY"),
            auto_retry=not args.stop_on_throttle, cooldown_minutes=args.cooldown,
            skip_captions=args.no_captions, captions_only=args.captions_only)
    except (FileNotFoundError, RuntimeError) as err:
        print(err, file=sys.stderr)
        return 1
    print(f"Processed {r['processed']}, already done {r['skipped']}, left {r['remaining']}, "
          f"throttled {r['throttles']}x  {r['counts']}")
    if r["stopped"]:
        print("Stopped. Run the same command again to continue.")
        return 3
    return 0


def cmd_status(_args) -> int:
    settings = _settings()
    quota = _quota(settings)
    print(f"Quota used today: {quota.used()} / {settings['daily_quota_limit']}")
    for folder, count in Storage(ROOT / "data").counts().items():
        print(f"{folder:<14} {count} videos")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="collector")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="collect videos for a category")
    collect_sub = collect.add_subparsers(dest="category", required=True)
    inap = collect_sub.add_parser("inappropriate")
    inap.add_argument("--max-per-channel", type=int, help="override config/settings.yaml")
    inap.set_defaults(func=cmd_collect_inappropriate)
    normal = collect_sub.add_parser("normal")
    normal.add_argument("--max-per-channel", type=int, help="override config/settings.yaml")
    normal.set_defaults(func=cmd_collect_normal)
    collect_sub.add_parser("popular").set_defaults(func=cmd_collect_popular)
    kw = collect_sub.add_parser("normal-keywords", help="NORMAL_KIDS_VIDEO via config/sources/normal_keywords.yaml")
    kw.add_argument("--per-keyword", type=int, help="videos kept per keyword (default: config)")
    kw.add_argument("--target-total", type=int, help="stop once the normal folder holds this many videos")
    kw.add_argument("--max-keywords", type=int, help="search at most N keywords this run (100 units each)")
    kw.set_defaults(func=cmd_collect_normal_keywords)
    rand = collect_sub.add_parser("random")
    rand.add_argument("--target", type=int, help="total random videos wanted (default: config)")
    rand.add_argument("--max-searches", type=int, help="cap searches this run (100 units each)")
    rand.set_defaults(func=cmd_collect_random)

    label = sub.add_parser("label", help="labelling workflow (Label Studio)")
    label_sub = label.add_subparsers(dest="label_command", required=True)
    exp = label_sub.add_parser("export", help="split new videos between reviewers")
    exp.add_argument("--per-reviewer", type=int,
                     help="assign only N videos per reviewer, split equally across the 4 categories")
    exp.add_argument("--only", nargs="+", help="give this batch only to these reviewers (e.g. a new one)")
    exp.set_defaults(func=cmd_label_export)
    label_sub.add_parser("rebuild", help="rewrite batch task files from existing assignments").set_defaults(
        func=cmd_label_rebuild)
    label_sub.add_parser("merge", help="merge Label Studio exports into labels.csv").set_defaults(func=cmd_label_merge)
    label_sub.add_parser("status", help="assigned vs labelled per reviewer").set_defaults(func=cmd_label_status)

    th = sub.add_parser("thumbnails", help="download thumbnails of labelled videos (no quota)")
    th.add_argument("--all", action="store_true", help="also download unlabelled videos in the dataset")
    th.set_defaults(func=cmd_thumbnails)

    en = sub.add_parser("enrich", help="captions + frames for labelled videos (yt-dlp, no quota, slow on purpose)")
    en.add_argument("--frames", type=int, default=16, help="evenly spaced frames kept per video (default 16)")
    en.add_argument("--max", type=int, help="process at most N videos this run")
    en.add_argument("--assigned", action="store_true",
                    help="every video given to a reviewer, labelled or not (use to scrape ahead of labelling)")
    en.add_argument("--ids", nargs="+", help="only these video IDs (default: every labelled video)")
    en.add_argument("--request-delay", type=float, nargs=2, default=(1, 2), metavar=("MIN", "MAX"))
    en.add_argument("--video-delay", type=float, nargs=2, default=(5, 10), metavar=("MIN", "MAX"))
    en.add_argument("--cooldown", type=float, default=30, metavar="MIN",
                    help="when YouTube throttles, wait this many minutes then continue by itself (doubles each time, max 4 h)")
    en.add_argument("--stop-on-throttle", action="store_true", help="stop at the first throttle instead of waiting and retrying")
    en.add_argument("--no-captions", action="store_true",
                    help="frames only (no caption downloads, which YouTube throttles most). Those videos get status "
                         "captions_throttled and are redone with captions by a later normal run")
    en.add_argument("--captions-only", action="store_true",
                    help="only fetch captions for videos marked captions_throttled; their frames are kept as they are")
    en.set_defaults(func=cmd_enrich)

    sub.add_parser("status").set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
