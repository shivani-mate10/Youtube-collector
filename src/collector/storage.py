"""Per-category folders: data/<folder>/{raw/, videos.jsonl, videos.csv, unavailable.jsonl}.

A video lives in exactly one category folder (the first one that found it). If another
category finds it later, that category is added to the stored record's source lists.
"""
import csv
import json
import os
from pathlib import Path

from .models import CollectionCategory

CSV_FIELDS = [
    "video_id", "video_url", "title", "description", "thumbnail_url",
    "channel_id", "channel_name", "published_at", "tags",
    "view_count", "like_count", "comment_count",
    "duration_seconds", "duration_iso", "live_broadcast_content", "made_for_kids",
    "collection_category", "collection_date", "last_fetched_at",
    "annotation_status", "availability_status", "source_categories", "sources",
]
_JSON_IN_CSV = {"tags", "source_categories", "sources"}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.state_dir = self.data_dir / "state"
        self._cache: dict[str, dict[str, dict]] = {}
        self._raw_seq = 0

    # ---- paths -------------------------------------------------------
    def folder(self, category: CollectionCategory | str) -> Path:
        name = category.folder if isinstance(category, CollectionCategory) else category
        path = self.data_dir / name
        (path / "raw").mkdir(parents=True, exist_ok=True)
        return path

    # ---- raw ---------------------------------------------------------
    def write_raw(self, category: CollectionCategory, run_id: str, name: str, params: dict, response: dict) -> Path:
        self._raw_seq += 1
        path = self.folder(category) / "raw" / f"{run_id}_{self._raw_seq:04d}_{name}.json"
        path.write_text(json.dumps({"params": params, "response": response}, ensure_ascii=False), encoding="utf-8")
        return path

    # ---- index (video_id -> home folder) -------------------------------
    def _index_path(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir / "video_index.json"

    def _load_index(self) -> dict:
        try:
            return json.loads(self._index_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    # ---- processed videos ----------------------------------------------
    def _records(self, folder: str) -> dict[str, dict]:
        if folder not in self._cache:
            path = self.folder(folder) / "videos.jsonl"
            records: dict[str, dict] = {}
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rec = json.loads(line)
                        records[rec["video_id"]] = rec
            self._cache[folder] = records
        return self._cache[folder]

    def _flush(self, folder: str) -> None:
        records = list(self._records(folder).values())
        base = self.folder(folder)
        _atomic_write(base / "videos.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        tmp = base / "videos.csv.tmp"
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for rec in records:
                writer.writerow({
                    k: json.dumps(v, ensure_ascii=False) if k in _JSON_IN_CSV else v
                    for k, v in rec.items()
                })
        os.replace(tmp, base / "videos.csv")

    def upsert_videos(self, category: CollectionCategory, records: list[dict]) -> dict:
        index = self._load_index()
        touched: set[str] = set()
        added = updated = 0
        for rec in records:
            vid = rec["video_id"]
            home = index.get(vid, {}).get("home", category.folder)
            existing = self._records(home).get(vid)
            if existing:
                updated += 1
                rec = {
                    **rec,
                    "collection_category": existing["collection_category"],
                    "collection_date": existing["collection_date"],
                    "annotation_status": existing["annotation_status"],
                    "source_categories": sorted(set(existing["source_categories"]) | set(rec["source_categories"])),
                    "sources": existing["sources"] + [s for s in rec["sources"] if s not in existing["sources"]],
                }
            else:
                added += 1
            self._records(home)[vid] = rec
            index[vid] = {"home": home, "categories": rec["source_categories"]}
            touched.add(home)
        for folder in touched:
            self._flush(folder)
        _atomic_write(self._index_path(), json.dumps(index, indent=1))
        return {"added": added, "updated": updated}

    # ---- unavailable ----------------------------------------------------
    def write_unavailable(self, category: CollectionCategory, records: list[dict]) -> int:
        if not records:
            return 0
        path = self.folder(category) / "unavailable.jsonl"
        existing: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    existing[rec["video_id"]] = rec
        for rec in records:
            existing[rec["video_id"]] = rec
        _atomic_write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in existing.values()))
        return len(records)

    # ---- keyword progress (so a keyword run can resume after the daily quota) ----
    def _progress_path(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir / "keyword_progress.json"

    def done_keywords(self, category: CollectionCategory) -> set[str]:
        try:
            data = json.loads(self._progress_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        return set(data.get(category.value, []))

    def mark_keyword_done(self, category: CollectionCategory, keyword: str) -> None:
        try:
            data = json.loads(self._progress_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        data.setdefault(category.value, [])
        if keyword not in data[category.value]:
            data[category.value].append(keyword)
        _atomic_write(self._progress_path(), json.dumps(data, indent=1, ensure_ascii=False))

    def known_ids(self) -> set[str]:
        """Every video ID already stored in any category."""
        return set(self._load_index())

    def counts(self) -> dict[str, int]:
        return {c.folder: len(self._records(c.folder)) for c in CollectionCategory}
