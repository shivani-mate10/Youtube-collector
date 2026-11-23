import csv
import urllib.request
from pathlib import Path


def download_labelled(data_dir: Path, only_labelled: bool = True) -> dict:
    """Download thumbnails (no API quota used) for rows in data/final/dataset.csv.

    Saved as data/thumbnails/<video_id>.jpg; already-downloaded files are skipped.
    """
    dataset = data_dir / "final" / "dataset.csv"
    if not dataset.exists():
        raise FileNotFoundError("data/final/dataset.csv not found. Run `collector label merge` first.")
    out = data_dir / "thumbnails"
    out.mkdir(parents=True, exist_ok=True)
    result = {"downloaded": 0, "skipped": 0, "failed": []}
    with dataset.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if only_labelled and not (row.get("binary_label") or "").strip():  # skips unlabelled and "Cannot review"
                continue
            url, path = row.get("thumbnail_url"), out / f"{row['video_id']}.jpg"
            if path.exists():
                result["skipped"] += 1
                continue
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    path.write_bytes(resp.read())
                result["downloaded"] += 1
            except Exception as err:  # keep going; report at the end
                result["failed"].append((row["video_id"], str(err)))
    return result
