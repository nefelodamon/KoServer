from dataclasses import dataclass
from typing import Optional


@dataclass
class KoLibraryDevice:
    id: int
    name: str
    friendly_name: str
    host: str
    port: int
    username: str
    books_path: str
    sync_interval: str  # manual, hourly, 6h, daily, weekly
    last_sync: Optional[str]
    created_at: str

    @property
    def display_name(self) -> str:
        return self.friendly_name.strip() if self.friendly_name and self.friendly_name.strip() else self.name


@dataclass
class SyncLog:
    id: int
    device_id: int
    started_at: str
    finished_at: Optional[str]
    status: str  # running, success, error
    books_added: int
    books_updated: int
    message: str


@dataclass
class KoBook:
    id: int
    device_id: int
    device_display_name: str
    file_path: str
    file_mtime: int
    title: str
    authors: str
    series: str
    series_index: Optional[float]
    language: str
    pages: int
    description: str
    cover_file: Optional[str]  # relative path under covers_dir
    progress_pct: float  # 0.0 – 1.0
    last_synced_at: str
