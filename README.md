# ScreenDetox YouTube Collector

Collects YouTube video metadata (YouTube Data API v3) for the ScreenDetox dataset.
Separate from the Django backend. Only the INAPPROPRIATE category is implemented so far.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then put your key in .env: YOUTUBE_API_KEY=...
```

## Use

```bash
collector collect inappropriate            # newest 30 videos per channel (config/settings.yaml)
collector collect normal                   # channels in config/sources/normal_channels.yaml
collector collect popular                  # mostPopular chart per region (settings.yaml)
collector collect random                   # random word + random date window search (100 units per search)
collector collect inappropriate --max-per-channel 10
collector status                           # quota used today + videos per category
pytest
```

## Output

`data/<category>/` where category is `inappropriate`, `normal`, `random` or `popular`:

- `videos.csv` / `videos.jsonl`: one row per video, `annotation_status = PENDING`
- `unavailable.jsonl`: videos that were private, deleted or not returned by the API
- `raw/`: untouched API responses
- `data/state/`: quota ledger, video-ID index (dedupe), run summaries
- `data/labels/`: reserved for manual labels (kept separate from collected data)

A video is stored once, in the first category folder that found it; later categories are
added to its `source_categories` / `sources`.

## Notes

- `collection_category` is a provisional source tag, not a confirmed label.
- Free quota is 10,000 units/day; a run uses about 40. Only 1-unit calls are used.
- YouTube API policy: stored API metadata should be refreshed or deleted within about 30 days.
  Keep video IDs and your own labels long term.

## Labelling (free Label Studio, one project per reviewer)

Reviewers are listed in `config/labeling.yaml`. Each video goes to exactly one reviewer.

1. `collector label export` assigns every not-yet-assigned video and writes one task file per reviewer
   to `data/labels/batches/batch_NNN/<reviewer>.json` (no collection category inside), plus
   `data/labels/label_studio_config.xml`. Run it again whenever new videos were collected: only the
   new ones are assigned (a new batch); earlier assignments never change.
2. In Label Studio: create a project per reviewer, paste `label_studio_config.xml` in
   Settings > Labeling Interface, and import that reviewer's batch file.
3. When labelling, export each project as **JSON** to `data/labels/raw/<reviewer>.json`
   (e.g. `shivani.json`), then run `collector label merge`.
4. `merge` writes `data/labels/labels.csv`, `data/final/dataset.csv` (one row per video with the
   label) and prints a category-by-label table. `collector label status` shows progress.

## Shared Label Studio server

Settings live in `label_studio/label-studio.env` (public address, trusted origin, secure cookies).
Start it with `label_studio/start.sh` (stop any running copy first). Label Studio's data stays in
`~/.local/share/label-studio`, so projects and accounts survive restarts.

## Captions and frames (`collector enrich`)

Run after `collector label merge`. For every labelled video it fetches English captions and YouTube's
seek-bar preview frames with yt-dlp (no API quota; paced slowly on purpose), keeps 16 evenly spaced frames
and deletes the rest. Needs Node.js or Deno on the PATH (YouTube challenge solver).

```bash
collector enrich                # all labelled videos not done yet (stop with Ctrl+C, re-run to continue)
collector enrich --max 20       # small batch
collector enrich --frames 24    # more frames per video
```

Output in `data/enrichment/<video_id>/`: `frames/*.jpg`, `subtitles.json`, `transcript.txt`, `metadata.json`,
`enrich.json`; summary table `data/enrichment/index.csv` (join on `video_id`; status `ok`, `no_captions`,
`no_frames`, `nothing_found`, `failed`). `failed` videos are retried on the next run.

A video throttled once is retried after a wait; throttled twice, it is retried without captions (status
`captions_throttled`: frames kept, captions fetched on a later run); throttled three times, it is set aside
(`throttled_skip`). Both statuses are retried on the next run, after all other videos.

Frames only now, captions later: `collector enrich --no-captions` (status `captions_throttled`), then
`collector enrich --captions-only` fetches just the captions for those videos and keeps their frames.

If YouTube throttles, the run waits by itself (30 min, then 60, 120, 240 max, with slower pacing each time) and
continues with the same video until all are done. Ctrl+C stops it; run the same command to continue.
`--cooldown MIN` sets the first wait; `--stop-on-throttle` stops instead of waiting. Keep the laptop awake, e.g.
`systemd-inhibit --what=sleep collector enrich`.
