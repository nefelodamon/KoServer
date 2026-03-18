from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Character:
    id: Optional[int]
    book_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    role: str = "unknown"
    occupation: str = ""
    physical_description: str = ""
    personality: str = ""
    relationships: list[str] = field(default_factory=list)
    first_appearance_quote: str = ""
    user_notes: str = ""
    portrait_file: str = ""
    source_page: Optional[int] = None
    first_seen_page: Optional[int] = None
    unlocked: bool = True
    needs_cleanup: bool = False


@dataclass
class Book:
    id: Optional[int]
    book_id: str
    title: str
    context: str = ""
    uploaded_at: str = ""
    deleted_at: Optional[str] = None
    character_count: int = 0
    # book_meta.json fields
    authors: str = ""
    series: str = ""
    series_index: Optional[float] = None
    language: str = ""
    description: str = ""
    identifiers: dict = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    total_pages: Optional[int] = None
    percent_finished: Optional[float] = None
    reading_status: str = ""
    last_read: str = ""
    highlights: Optional[int] = None
    notes: Optional[int] = None
    partial_md5: str = ""
    cover_filename: str = ""
