"""Every category source yields Candidates; the rest of the pipeline is shared."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


@dataclass
class Candidate:
    video_id: str
    source_type: str  # e.g. "channel_uploads"
    source_id: str  # e.g. the channel ID
    source_name: str
    # Set when the source itself already knows the video is private or deleted.
    unavailable_reason: str | None = None


class VideoSource(ABC):
    @abstractmethod
    def candidates(self) -> Iterator[Candidate]:
        ...
