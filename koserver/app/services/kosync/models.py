from dataclasses import dataclass
from typing import Optional


@dataclass
class KoSyncUser:
    id: Optional[int]
    username: str
    created_at: str = ""
    last_sync: Optional[str] = None
    document_count: int = 0


@dataclass
class ReadingProgress:
    id: Optional[int]
    username: str
    document: str
    progress: str
    percentage: float
    device: str
    device_id: str
    timestamp: int
    updated_at: str = ""
