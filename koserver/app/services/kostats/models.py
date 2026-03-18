from dataclasses import dataclass, field
from typing import Optional


@dataclass
class KoStatsUser:
    id: Optional[int]
    username: str
    created_at: str = ""
    last_upload: Optional[str] = None
    file_count: int = 0


@dataclass
class UploadedFile:
    name: str
    path: str          # relative to user's directory
    size: int
    modified: str
