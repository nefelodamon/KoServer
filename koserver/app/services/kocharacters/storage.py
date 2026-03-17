import json
import sqlite3
from pathlib import Path

from app.services.kocharacters.models import Book, Character

# Use synchronous sqlite3 — simple and sufficient for this workload.


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


async def init_db(db_path: Path) -> None:
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS books (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id     TEXT    NOT NULL UNIQUE,
            title       TEXT    NOT NULL,
            context     TEXT    NOT NULL DEFAULT '',
            uploaded_at TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS characters (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id              TEXT    NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
            name                 TEXT    NOT NULL,
            aliases              TEXT    NOT NULL DEFAULT '[]',
            role                 TEXT    NOT NULL DEFAULT 'unknown',
            occupation           TEXT    NOT NULL DEFAULT '',
            physical_description TEXT    NOT NULL DEFAULT '',
            personality          TEXT    NOT NULL DEFAULT '',
            relationships        TEXT    NOT NULL DEFAULT '[]',
            first_appearance_quote TEXT  NOT NULL DEFAULT '',
            user_notes           TEXT    NOT NULL DEFAULT '',
            portrait_file        TEXT    NOT NULL DEFAULT '',
            source_page          INTEGER,
            first_seen_page      INTEGER,
            unlocked             INTEGER NOT NULL DEFAULT 1,
            needs_cleanup        INTEGER NOT NULL DEFAULT 0,
            UNIQUE(book_id, name)
        );
    """)
    conn.commit()
    conn.close()


def _row_to_book(row: sqlite3.Row, character_count: int = 0) -> Book:
    return Book(
        id=row["id"],
        book_id=row["book_id"],
        title=row["title"],
        context=row["context"],
        uploaded_at=row["uploaded_at"],
        character_count=character_count,
    )


def _row_to_character(row: sqlite3.Row) -> Character:
    return Character(
        id=row["id"],
        book_id=row["book_id"],
        name=row["name"],
        aliases=json.loads(row["aliases"]),
        role=row["role"],
        occupation=row["occupation"],
        physical_description=row["physical_description"],
        personality=row["personality"],
        relationships=json.loads(row["relationships"]),
        first_appearance_quote=row["first_appearance_quote"],
        user_notes=row["user_notes"],
        portrait_file=row["portrait_file"],
        source_page=row["source_page"],
        first_seen_page=row["first_seen_page"],
        unlocked=bool(row["unlocked"]),
        needs_cleanup=bool(row["needs_cleanup"]),
    )


def upsert_book(db_path: Path, book_id: str, title: str, context: str) -> None:
    conn = _connect(db_path)
    conn.execute("""
        INSERT INTO books (book_id, title, context, uploaded_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(book_id) DO UPDATE SET
            title       = excluded.title,
            context     = excluded.context,
            uploaded_at = excluded.uploaded_at
    """, (book_id, title, context))
    conn.commit()
    conn.close()


def upsert_characters(db_path: Path, book_id: str, characters: list[dict]) -> None:
    conn = _connect(db_path)
    # Delete characters no longer in the upload, then upsert present ones.
    incoming_names = [c.get("name", "") for c in characters if c.get("name")]
    if incoming_names:
        placeholders = ",".join("?" * len(incoming_names))
        conn.execute(
            f"DELETE FROM characters WHERE book_id = ? AND name NOT IN ({placeholders})",
            [book_id, *incoming_names],
        )
    else:
        conn.execute("DELETE FROM characters WHERE book_id = ?", (book_id,))

    for c in characters:
        name = c.get("name", "").strip()
        if not name:
            continue
        conn.execute("""
            INSERT INTO characters (
                book_id, name, aliases, role, occupation,
                physical_description, personality, relationships,
                first_appearance_quote, user_notes, portrait_file,
                source_page, first_seen_page, unlocked, needs_cleanup
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(book_id, name) DO UPDATE SET
                aliases              = excluded.aliases,
                role                 = excluded.role,
                occupation           = excluded.occupation,
                physical_description = excluded.physical_description,
                personality          = excluded.personality,
                relationships        = excluded.relationships,
                first_appearance_quote = excluded.first_appearance_quote,
                user_notes           = CASE
                                         WHEN excluded.user_notes != ''
                                         THEN excluded.user_notes
                                         ELSE characters.user_notes
                                       END,
                portrait_file        = excluded.portrait_file,
                source_page          = excluded.source_page,
                first_seen_page      = excluded.first_seen_page,
                unlocked             = excluded.unlocked,
                needs_cleanup        = excluded.needs_cleanup
        """, (
            book_id,
            name,
            json.dumps(c.get("aliases") or []),
            c.get("role") or "unknown",
            c.get("occupation") or "",
            c.get("physical_description") or "",
            c.get("personality") or "",
            json.dumps(c.get("relationships") or []),
            c.get("first_appearance_quote") or "",
            c.get("user_notes") or "",
            c.get("portrait_file") or "",
            c.get("source_page"),
            c.get("first_seen_page"),
            1 if c.get("unlocked", True) else 0,
            1 if c.get("needs_cleanup", False) else 0,
        ))
    conn.commit()
    conn.close()


def list_books(db_path: Path) -> list[Book]:
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT b.*, COUNT(c.id) AS character_count
        FROM books b
        LEFT JOIN characters c ON c.book_id = b.book_id
        GROUP BY b.id
        ORDER BY b.uploaded_at DESC
    """).fetchall()
    conn.close()
    return [_row_to_book(r, r["character_count"]) for r in rows]


def get_book(db_path: Path, book_id: str) -> Book | None:
    conn = _connect(db_path)
    row = conn.execute("""
        SELECT b.*, COUNT(c.id) AS character_count
        FROM books b
        LEFT JOIN characters c ON c.book_id = b.book_id
        WHERE b.book_id = ?
        GROUP BY b.id
    """, (book_id,)).fetchone()
    conn.close()
    return _row_to_book(row, row["character_count"]) if row else None


def get_characters(db_path: Path, book_id: str) -> list[Character]:
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT * FROM characters WHERE book_id = ?
        ORDER BY first_seen_page ASC NULLS LAST, name ASC
    """, (book_id,)).fetchall()
    conn.close()
    return [_row_to_character(r) for r in rows]


def delete_book(db_path: Path, book_id: str) -> bool:
    conn = _connect(db_path)
    cur = conn.execute("DELETE FROM books WHERE book_id = ?", (book_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0
