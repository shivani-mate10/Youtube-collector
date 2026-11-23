"""Random sampling: a random query word in a random short publish-date window.

YouTube has no "random video" endpoint, so this is a pseudo-random sample (search ranking
still influences which videos come back inside a window).
"""
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

CAMERA_PREFIXES = ("IMG", "MOV", "VID", "DSC", "PXL")


def load_words(path: Path) -> list[str]:
    lines = (line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines())
    return [w for w in lines if w and not w.startswith("#")]


def make_query(words: list[str], rng: random.Random) -> str:
    """70%: one random word. 30%: a camera-style default title (e.g. 'IMG 4821'), which finds
    ordinary uploads that no one titled or optimised."""
    if rng.random() < 0.3:
        return f"{rng.choice(CAMERA_PREFIXES)} {rng.randint(1000, 9999)}"
    return rng.choice(words)


def random_window(start: date, window_days: int, rng: random.Random, today: date | None = None) -> tuple[str, str]:
    """Return RFC 3339 (publishedAfter, publishedBefore) for a random window ending before today."""
    today = today or datetime.now(timezone.utc).date()
    latest_start = today - timedelta(days=window_days)
    span = (latest_start - start).days
    if span < 0:
        raise ValueError("window_start is too recent for the requested window_days")
    first = start + timedelta(days=rng.randint(0, span))
    begin = datetime.combine(first, time.min, tzinfo=timezone.utc)
    end = begin + timedelta(days=window_days)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return begin.strftime(fmt), end.strftime(fmt)
