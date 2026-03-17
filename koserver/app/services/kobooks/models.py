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
    character_count: int = 0
