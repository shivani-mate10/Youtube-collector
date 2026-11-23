"""Labelling workflow without paid Label Studio features.

export: split not-yet-assigned videos between reviewers -> one Label Studio task file each.
merge:  read each reviewer's Label Studio export -> labels.csv + the final dataset.
The collection category is never put in a task, so reviewers can't be influenced by it.
"""
import csv
import json
import random
from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from .models import AnnotationStatus, CollectionCategory
from .storage import CSV_FIELDS

LABELS = ["Suitable", "Disturbing", "Restricted", "Irrelevant"]
CANNOT_REVIEW = "Cannot review"
# 2-label view of the 4 labels (same mapping as the ElsaGate paper)
BINARY = {"Suitable": "Appropriate", "Irrelevant": "Appropriate",
          "Disturbing": "Inappropriate", "Restricted": "Inappropriate"}
REASONS = ["violence", "sexual content", "scary / horror", "profanity", "gambling",
           "drugs / alcohol", "crude humour", "other"]

ASSIGN_FIELDS = ["video_id", "reviewer", "batch", "assigned_at"]
LABEL_FIELDS = ["video_id", "reviewer", "label", "reason_tags", "notes", "labelled_at"]
FINAL_EXTRA = ["final_label", "binary_label", "reason_tags", "reviewer", "labelled_at"]
_JSON_IN_CSV = {"tags", "source_categories", "sources"}

LABEL_CONFIG = f"""<View>
  <HyperText name="player" value="$embed" inline="false"/>
  <Header value="$title"/>
  <Text name="meta" value="$meta"/>
  <Text name="description" value="$description"/>
  <Header value="Label"/>
  <Choices name="label" toName="player" choice="single-radio" showInline="true" required="true">
{chr(10).join(f'    <Choice value="{v}"/>' for v in LABELS + [CANNOT_REVIEW])}
  </Choices>
  <Header value="Reasons (optional)"/>
  <Choices name="reasons" toName="player" choice="multiple" showInline="true">
{chr(10).join(f'    <Choice value="{v}"/>' for v in REASONS)}
  </Choices>
  <TextArea name="notes" toName="player" rows="2" placeholder="Notes (optional)"/>
</View>
"""


def slug(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


# ---- reading collected data ---------------------------------------------------
def load_videos(data_dir: Path) -> dict[str, dict]:
    videos: dict[str, dict] = {}
    for category in CollectionCategory:
        path = Path(data_dir) / category.folder / "videos.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    videos[rec["video_id"]] = rec
    return videos


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---- export: assign + write Label Studio tasks -----------------------------------
def _duration(seconds) -> str:
    if seconds is None or seconds == "":
        return "unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def make_task(rec: dict) -> dict:
    """Only what a reviewer needs. No collection category, keyword, channel group or source."""
    vid = rec["video_id"]
    embed = (
        f'<iframe width="640" height="360" src="https://www.youtube.com/embed/{escape(vid)}" '
        f'frameborder="0" allowfullscreen></iframe>'
        f'<p><a href="{escape(rec["video_url"])}" target="_blank">Open on YouTube</a> '
        f"(if the player says the video is unavailable)</p>"
    )
    return {"data": {
        "video_id": vid,
        "video_url": rec["video_url"],
        "embed": embed,
        "title": rec.get("title") or "",
        "description": (rec.get("description") or "")[:1500],
        "meta": (f"Duration: {_duration(rec.get('duration_seconds'))} | "
                 f"Published: {(rec.get('published_at') or '')[:10]} | "
                 f"Made for kids (set by uploader): {_made_for_kids(rec.get('made_for_kids'))}"),
    }}


def _made_for_kids(flag) -> str:
    return {True: "Yes", False: "No"}.get(flag, "Unknown")


def rebuild_batches(data_dir: Path) -> dict[str, int]:
    """Rewrite every batch task file from assignments.csv (same assignments, current task layout)."""
    labels_dir = Path(data_dir) / "labels"
    videos = load_videos(data_dir)
    groups: dict[tuple[str, str], list[str]] = {}
    for row in _read_csv(labels_dir / "assignments.csv"):
        groups.setdefault((row["batch"], row["reviewer"]), []).append(row["video_id"])
    (labels_dir / "label_studio_config.xml").write_text(LABEL_CONFIG, encoding="utf-8")
    written = {}
    for (batch, reviewer), ids in sorted(groups.items()):
        path = labels_dir / "batches" / f"batch_{int(batch):03d}" / f"{reviewer}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tasks = [make_task(videos[v]) for v in ids if v in videos]
        path.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")
        written[f"batch_{int(batch):03d}/{reviewer}"] = len(tasks)
    return written


def assign_new(videos: dict, assigned_ids: set, counts: Counter, reviewers: list[str], rng: random.Random,
               batch: int, now: str, per_reviewer: int | None = None) -> list[dict]:
    """Give each not-yet-assigned video to the reviewer with the fewest videos so far.

    With per_reviewer=N, assign only N videos per reviewer, split equally across the four
    collection categories (N // 4 each), so every reviewer sees the same category mix.
    """
    pool: dict[str, list[str]] = {}
    for vid, rec in videos.items():
        if vid not in assigned_ids:
            pool.setdefault(rec.get("collection_category"), []).append(vid)

    if per_reviewer is None:
        groups = [([v for ids in pool.values() for v in ids], None)]
    else:
        per_category = (per_reviewer // len(CollectionCategory)) * len(reviewers)
        groups = [(ids, per_category) for _, ids in sorted(pool.items())]

    rows = []
    for ids, limit in groups:
        rng.shuffle(ids)
        in_group: Counter = Counter()
        for vid in ids[:limit]:
            reviewer = min(reviewers, key=lambda r: (in_group[r] if limit else 0, counts[r], rng.random()))
            in_group[reviewer] += 1
            counts[reviewer] += 1
            rows.append({"video_id": vid, "reviewer": reviewer, "batch": batch, "assigned_at": now})
    return rows


def export_tasks(data_dir: Path, reviewers: list[str], rng: random.Random | None = None,
                 per_reviewer: int | None = None, only: list[str] | None = None) -> dict:
    """`only` limits this batch to some reviewers (e.g. a newly added one); others get nothing."""
    rng = rng or random.Random()
    labels_dir = Path(data_dir) / "labels"
    assign_path = labels_dir / "assignments.csv"
    videos = load_videos(data_dir)
    existing = _read_csv(assign_path)
    ids = [slug(r) for r in reviewers]
    counts = Counter({i: 0 for i in ids})
    counts.update(r["reviewer"] for r in existing if r["reviewer"] in counts)
    batch = max((int(r["batch"]) for r in existing), default=0) + 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    active = [slug(n) for n in only] if only else ids
    unknown = [a for a in active if a not in ids]
    if unknown:
        raise ValueError(f"Not in config/labeling.yaml: {', '.join(unknown)}")
    rows = assign_new(videos, {r["video_id"] for r in existing}, counts, active, rng, batch, now, per_reviewer)
    result = {"batch": batch, "new": len(rows), "files": {}, "per_reviewer": Counter(r["reviewer"] for r in rows),
              "per_category": Counter((r["reviewer"], videos[r["video_id"]]["collection_category"]) for r in rows)}
    labels_dir.mkdir(parents=True, exist_ok=True)
    (labels_dir / "label_studio_config.xml").write_text(LABEL_CONFIG, encoding="utf-8")
    if not rows:
        return result

    batch_dir = labels_dir / "batches" / f"batch_{batch:03d}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    for reviewer in active:
        tasks = [make_task(videos[r["video_id"]]) for r in rows if r["reviewer"] == reviewer]
        path = batch_dir / f"{reviewer}.json"
        path.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")
        result["files"][reviewer] = path
    _write_csv(assign_path, ASSIGN_FIELDS, existing + rows)
    return result


# ---- merge: Label Studio exports -> labels.csv + final dataset ---------------------
def parse_export(path: Path) -> list[dict]:
    """Read one reviewer's Label Studio JSON export. The reviewer is the file name."""
    reviewer = slug(Path(path).stem)
    rows = []
    for task in json.loads(Path(path).read_text(encoding="utf-8")):
        vid = task.get("data", {}).get("video_id")
        valid = [a for a in task.get("annotations", []) if not a.get("was_cancelled")]
        if not vid or not valid:
            continue
        ann = max(valid, key=lambda a: a.get("updated_at") or a.get("created_at") or "")
        label, reasons, notes = None, [], ""
        for res in ann.get("result", []):
            value = res.get("value", {})
            if res.get("from_name") == "label" and value.get("choices"):
                label = value["choices"][0]
            elif res.get("from_name") == "reasons":
                reasons = value.get("choices", [])
            elif res.get("from_name") == "notes":
                notes = " ".join(value.get("text", []))
        if label:
            rows.append({"video_id": vid, "reviewer": reviewer, "label": label,
                         "reason_tags": json.dumps(reasons, ensure_ascii=False), "notes": notes,
                         "labelled_at": ann.get("updated_at") or ann.get("created_at") or ""})
    return rows


def merge_labels(data_dir: Path) -> dict:
    data_dir = Path(data_dir)
    labels_dir = data_dir / "labels"
    exports = sorted((labels_dir / "raw").glob("*.json"))
    if not exports:
        raise FileNotFoundError(f"No Label Studio exports found in {labels_dir / 'raw'}")

    assigned = {r["video_id"]: r["reviewer"] for r in _read_csv(labels_dir / "assignments.csv")}
    labels: dict[str, dict] = {r["video_id"]: r for r in _read_csv(labels_dir / "labels.csv")}
    warnings = []
    for path in exports:
        for row in parse_export(path):
            if assigned.get(row["video_id"]) not in (None, row["reviewer"]):
                warnings.append(f"{row['video_id']} was assigned to {assigned[row['video_id']]} "
                                f"but labelled by {row['reviewer']} (kept)")
            labels[row["video_id"]] = row
    _write_csv(labels_dir / "labels.csv", LABEL_FIELDS, list(labels.values()))

    videos = load_videos(data_dir)
    final_rows, crosstab = [], Counter()
    for vid, rec in videos.items():
        lab = labels.get(vid)
        row = {**rec, "final_label": None, "binary_label": None, "reason_tags": None, "reviewer": None, "labelled_at": None}
        if lab:
            row.update(final_label=lab["label"], binary_label=BINARY.get(lab["label"]), reason_tags=lab["reason_tags"], reviewer=lab["reviewer"],
                       labelled_at=lab["labelled_at"],
                       annotation_status="CANNOT_REVIEW" if lab["label"] == CANNOT_REVIEW else "LABELLED")
            crosstab[(rec["collection_category"], lab["label"])] += 1
        else:
            row["annotation_status"] = AnnotationStatus.PENDING.value
        final_rows.append({k: json.dumps(v, ensure_ascii=False) if k in _JSON_IN_CSV else v for k, v in row.items()})
    _write_csv(data_dir / "final" / "dataset.csv", CSV_FIELDS + FINAL_EXTRA, final_rows)
    return {"labelled": len(labels), "total": len(videos), "crosstab": crosstab, "warnings": warnings}


def status(data_dir: Path, reviewers: list[str]) -> dict[str, tuple[int, int]]:
    """{reviewer: (assigned, labelled)} from assignments.csv and labels.csv."""
    labels_dir = Path(data_dir) / "labels"
    assigned = Counter(r["reviewer"] for r in _read_csv(labels_dir / "assignments.csv"))
    done = Counter(r["reviewer"] for r in _read_csv(labels_dir / "labels.csv"))
    return {slug(r): (assigned[slug(r)], done[slug(r)]) for r in reviewers}
