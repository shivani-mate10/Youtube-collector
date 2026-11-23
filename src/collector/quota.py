"""Daily quota ledger. The YouTube quota resets at midnight Pacific time."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Units charged per call.
COST_CHANNELS_LIST = 1
COST_PLAYLIST_ITEMS_LIST = 1
COST_VIDEOS_LIST = 1
COST_SEARCH_LIST = 100  # only used by the RANDOM_VIDEO sampler


class QuotaExceeded(Exception):
    pass


class QuotaTracker:
    def __init__(self, path: Path, daily_limit: int = 10000, safety_margin: int = 500):
        self.path = Path(path)
        self.daily_limit = daily_limit
        self.safety_margin = safety_margin

    @staticmethod
    def _today() -> str:
        return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            data = {}
        if data.get("date") != self._today():
            data = {"date": self._today(), "used": 0}
        return data

    def used(self) -> int:
        return self._load()["used"]

    def remaining(self) -> int:
        return max(0, self.daily_limit - self.safety_margin - self.used())

    def charge(self, units: int) -> None:
        data = self._load()
        if data["used"] + units > self.daily_limit - self.safety_margin:
            raise QuotaExceeded(
                f"Quota guard: {data['used']} units used today, limit "
                f"{self.daily_limit} (margin {self.safety_margin}). Resume after midnight Pacific."
            )
        data["used"] += units
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))
