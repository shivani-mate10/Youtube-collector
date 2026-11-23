from enum import Enum


class CollectionCategory(str, Enum):
    INAPPROPRIATE = "INAPPROPRIATE"
    NORMAL_KIDS_VIDEO = "NORMAL_KIDS_VIDEO"
    RANDOM_VIDEO = "RANDOM_VIDEO"
    POPULAR_VIDEO = "POPULAR_VIDEO"

    @property
    def folder(self) -> str:
        return _FOLDERS[self]


_FOLDERS = {
    CollectionCategory.INAPPROPRIATE: "inappropriate",
    CollectionCategory.NORMAL_KIDS_VIDEO: "normal",
    CollectionCategory.RANDOM_VIDEO: "random",
    CollectionCategory.POPULAR_VIDEO: "popular",
}


class AnnotationStatus(str, Enum):
    PENDING = "PENDING"


class AvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
