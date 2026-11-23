import csv
import json
import random

from collector import labeling


def write_videos(data_dir, folder, ids, category):
    d = data_dir / folder
    d.mkdir(parents=True, exist_ok=True)
    recs = [{"video_id": i, "video_url": f"https://www.youtube.com/watch?v={i}", "title": i, "description": "d",
             "duration_seconds": 65, "published_at": "2026-01-01T00:00:00Z", "collection_category": category,
             "annotation_status": "PENDING", "tags": [], "source_categories": [category],
             "sources": [{"source_type": "x", "source_id": "SECRET-KEYWORD"}]} for i in ids]
    with (d / "videos.jsonl").open("a") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


REVIEWERS = ["Shivani", "Sampada", "Pratik", "Hrishikesh"]


def test_export_splits_uniquely_hides_source_and_adds_batches(tmp_path):
    write_videos(tmp_path, "inappropriate", [f"a{i}" for i in range(10)], "INAPPROPRIATE")
    write_videos(tmp_path, "normal", [f"n{i}" for i in range(10)], "NORMAL_KIDS_VIDEO")
    r1 = labeling.export_tasks(tmp_path, REVIEWERS, random.Random(1))
    assert r1["batch"] == 1 and r1["new"] == 20 and set(r1["per_reviewer"].values()) == {5}

    tasks = {name: json.loads(p.read_text()) for name, p in r1["files"].items()}
    all_ids = [t["data"]["video_id"] for ts in tasks.values() for t in ts]
    assert len(all_ids) == len(set(all_ids)) == 20  # each video goes to exactly one reviewer
    blob = json.dumps(tasks)
    assert "INAPPROPRIATE" not in blob and "NORMAL_KIDS" not in blob and "SECRET-KEYWORD" not in blob

    # New videos arrive later: only they are assigned, old assignments stay as they were.
    write_videos(tmp_path, "popular", [f"p{i}" for i in range(7)], "POPULAR_VIDEO")
    before = list(csv.DictReader((tmp_path / "labels" / "assignments.csv").open()))
    r2 = labeling.export_tasks(tmp_path, REVIEWERS, random.Random(2))
    assert r2["batch"] == 2 and r2["new"] == 7
    after = list(csv.DictReader((tmp_path / "labels" / "assignments.csv").open()))
    assert after[:20] == before and len(after) == 27
    counts = list(labeling.status(tmp_path, REVIEWERS).values())
    assert max(a for a, _ in counts) - min(a for a, _ in counts) <= 1  # stays balanced
    assert labeling.export_tasks(tmp_path, REVIEWERS)["new"] == 0  # nothing left to assign


def fake_export(video_ids, label, reasons=(), notes=""):
    return [{"data": {"video_id": v}, "annotations": [{
        "was_cancelled": False, "updated_at": "2026-11-10T10:00:00Z",
        "result": [
            {"from_name": "label", "type": "choices", "value": {"choices": [label]}},
            {"from_name": "reasons", "type": "choices", "value": {"choices": list(reasons)}},
            {"from_name": "notes", "type": "textarea", "value": {"text": [notes]}},
        ]}]} for v in video_ids]


def test_merge_builds_labels_and_final_dataset(tmp_path):
    write_videos(tmp_path, "inappropriate", ["a1", "a2"], "INAPPROPRIATE")
    write_videos(tmp_path, "random", ["r1", "r2"], "RANDOM_VIDEO")
    res = labeling.export_tasks(tmp_path, REVIEWERS, random.Random(3))
    mine = {name: [t["data"]["video_id"] for t in json.loads(p.read_text())] for name, p in res["files"].items()}

    raw = tmp_path / "labels" / "raw"
    raw.mkdir(parents=True)
    first = next(n for n, ids in mine.items() if ids)
    ids = mine[first]
    export = fake_export(ids[:1], "Restricted", ["violence"], "gore") + [
        {"data": {"video_id": "skipped"}, "annotations": [{"was_cancelled": True, "result": []}]}]
    (raw / f"{first}.json").write_text(json.dumps(export))

    out = labeling.merge_labels(tmp_path)
    assert out["labelled"] == 1 and out["total"] == 4 and not out["warnings"]
    rows = list(csv.DictReader((tmp_path / "labels" / "labels.csv").open()))
    assert rows[0]["label"] == "Restricted" and rows[0]["reviewer"] == first
    final = list(csv.DictReader((tmp_path / "final" / "dataset.csv").open()))
    done = [r for r in final if r["final_label"]]
    assert len(final) == 4 and len(done) == 1 and done[0]["annotation_status"] == "LABELLED"
    assert all(r["annotation_status"] == "PENDING" for r in final if not r["final_label"])
    cat = done[0]["collection_category"]
    assert out["crosstab"][(cat, "Restricted")] == 1


def test_per_reviewer_splits_equally_across_categories(tmp_path):
    write_videos(tmp_path, "inappropriate", [f"a{i}" for i in range(40)], "INAPPROPRIATE")
    write_videos(tmp_path, "normal", [f"n{i}" for i in range(40)], "NORMAL_KIDS_VIDEO")
    write_videos(tmp_path, "random", [f"r{i}" for i in range(40)], "RANDOM_VIDEO")
    write_videos(tmp_path, "popular", [f"p{i}" for i in range(40)], "POPULAR_VIDEO")
    res = labeling.export_tasks(tmp_path, REVIEWERS, random.Random(4), per_reviewer=8)
    assert res["new"] == 32 and set(res["per_reviewer"].values()) == {8}
    assert set(res["per_category"].values()) == {2}  # 8 // 4 per category, per reviewer
    # Later batch keeps going from the unassigned videos only.
    res2 = labeling.export_tasks(tmp_path, REVIEWERS, random.Random(5), per_reviewer=8)
    first = {r["video_id"] for r in labeling._read_csv(tmp_path / "labels" / "assignments.csv")}
    assert res2["batch"] == 2 and len(first) == 64


def test_only_gives_batch_to_new_reviewer_alone(tmp_path):
    import pytest
    for folder, cat, p in [("inappropriate", "INAPPROPRIATE", "a"), ("normal", "NORMAL_KIDS_VIDEO", "n"),
                           ("random", "RANDOM_VIDEO", "r"), ("popular", "POPULAR_VIDEO", "p")]:
        write_videos(tmp_path, folder, [f"{p}{i}" for i in range(30)], cat)
    labeling.export_tasks(tmp_path, REVIEWERS, random.Random(1), per_reviewer=8)
    team = REVIEWERS + ["Saurabh"]
    res = labeling.export_tasks(tmp_path, team, random.Random(2), per_reviewer=8, only=["Saurabh"])
    assert list(res["files"]) == ["saurabh"] and res["new"] == 8
    assert set(res["per_category"].values()) == {2}
    status = labeling.status(tmp_path, team)
    assert status["saurabh"][0] == 8 and all(status[slug][0] == 8 for slug in ("shivani", "pratik"))
    with pytest.raises(ValueError):
        labeling.export_tasks(tmp_path, team, only=["Nobody"])


def test_task_shows_made_for_kids_and_rebuild_keeps_assignments(tmp_path):
    write_videos(tmp_path, "normal", ["n1", "n2"], "NORMAL_KIDS_VIDEO")
    for line_path in [tmp_path / "normal" / "videos.jsonl"]:
        recs = [json.loads(l) for l in line_path.read_text().splitlines()]
        recs[0]["made_for_kids"], recs[1]["made_for_kids"] = True, None
        line_path.write_text("".join(json.dumps(r) + "\n" for r in recs))
    res = labeling.export_tasks(tmp_path, ["Shivani"], random.Random(1))
    before = (tmp_path / "labels" / "assignments.csv").read_text()
    out = labeling.rebuild_batches(tmp_path)
    assert out == {"batch_001/shivani": 2}
    assert (tmp_path / "labels" / "assignments.csv").read_text() == before
    metas = {t["data"]["video_id"]: t["data"]["meta"] for t in json.loads(res["files"]["shivani"].read_text())}
    assert "Made for kids (set by uploader): Yes" in metas["n1"] and "Unknown" in metas["n2"]
