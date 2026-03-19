import json
import sqlite3
from pathlib import Path

from app.services.kocharacters.models import Book, Character

THUMBNAIL_SIZE_KEY = "thumbnail_size"
DEFAULT_THUMBNAIL_SIZE = 400

# Use synchronous sqlite3 — simple and sufficient for this workload.


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _unique_is_name_based(conn: sqlite3.Connection) -> bool:
    """Return True if the characters table still has the old UNIQUE(book_id, name) constraint."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='characters'"
    ).fetchone()
    if not sql:
        return False
    return "UNIQUE(book_id, name)" in (sql[0] or "")


async def init_db(db_path: Path) -> None:
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kocharacters_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS books (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id          TEXT    NOT NULL UNIQUE,
            title            TEXT    NOT NULL,
            authors          TEXT    NOT NULL DEFAULT '',
            series           TEXT    NOT NULL DEFAULT '',
            series_index     REAL,
            language         TEXT    NOT NULL DEFAULT '',
            description      TEXT    NOT NULL DEFAULT '',
            identifiers      TEXT    NOT NULL DEFAULT '{}',
            keywords         TEXT    NOT NULL DEFAULT '[]',
            total_pages      INTEGER,
            percent_finished REAL,
            reading_status   TEXT    NOT NULL DEFAULT '',
            last_read        TEXT    NOT NULL DEFAULT '',
            highlights       INTEGER,
            notes            INTEGER,
            partial_md5      TEXT    NOT NULL DEFAULT '',
            cover_filename   TEXT    NOT NULL DEFAULT '',
            context          TEXT    NOT NULL DEFAULT '',
            uploaded_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            deleted_at       TEXT    DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS characters (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id              TEXT    NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
            character_id         TEXT    NOT NULL DEFAULT '',
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
            UNIQUE(book_id, character_id)
        );
    """)
    conn.commit()
    # Migration: recreate characters table to change UNIQUE constraint
    # from (book_id, name) to (book_id, character_id) and add character_id column.
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(characters)").fetchall()
    }
    if "character_id" not in existing_cols or _unique_is_name_based(conn):
        has_char_id = "character_id" in existing_cols
        char_id_expr = "CASE WHEN character_id != '' THEN character_id ELSE name END" if has_char_id else "name"
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS characters_new (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id              TEXT    NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
                character_id         TEXT    NOT NULL DEFAULT '',
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
                UNIQUE(book_id, character_id)
            );
            INSERT INTO characters_new
                SELECT id, book_id, {char_id_expr},
                    name, aliases, role, occupation, physical_description,
                    personality, relationships, first_appearance_quote,
                    user_notes, portrait_file, source_page, first_seen_page,
                    unlocked, needs_cleanup
                FROM characters;
            DROP TABLE characters;
            ALTER TABLE characters_new RENAME TO characters;
        """)
        conn.commit()
    # Migrations: add columns that may not exist in older databases
    _migrations = [
        "ALTER TABLE books ADD COLUMN deleted_at TEXT DEFAULT NULL",
        "ALTER TABLE books ADD COLUMN authors TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE books ADD COLUMN series TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE books ADD COLUMN series_index REAL",
        "ALTER TABLE books ADD COLUMN language TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE books ADD COLUMN description TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE books ADD COLUMN identifiers TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE books ADD COLUMN keywords TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE books ADD COLUMN total_pages INTEGER",
        "ALTER TABLE books ADD COLUMN percent_finished REAL",
        "ALTER TABLE books ADD COLUMN reading_status TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE books ADD COLUMN last_read TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE books ADD COLUMN highlights INTEGER",
        "ALTER TABLE books ADD COLUMN notes INTEGER",
        "ALTER TABLE books ADD COLUMN partial_md5 TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE books ADD COLUMN cover_filename TEXT NOT NULL DEFAULT ''",
    ]
    for sql in _migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
    conn.close()


def get_setting(db_path: Path, key: str, default: str = "") -> str:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT value FROM kocharacters_settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(db_path: Path, key: str, value: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO kocharacters_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def _row_to_book(row: sqlite3.Row, character_count: int = 0) -> Book:
    return Book(
        id=row["id"],
        book_id=row["book_id"],
        title=row["title"],
        context=row["context"],
        uploaded_at=row["uploaded_at"],
        deleted_at=row["deleted_at"],
        character_count=character_count,
        authors=row["authors"] or "",
        series=row["series"] or "",
        series_index=row["series_index"],
        language=row["language"] or "",
        description=row["description"] or "",
        identifiers=json.loads(row["identifiers"] or "{}"),
        keywords=json.loads(row["keywords"] or "[]"),
        total_pages=row["total_pages"],
        percent_finished=row["percent_finished"],
        reading_status=row["reading_status"] or "",
        last_read=row["last_read"] or "",
        highlights=row["highlights"],
        notes=row["notes"],
        partial_md5=row["partial_md5"] or "",
        cover_filename=row["cover_filename"] or "",
    )


def _row_to_character(row: sqlite3.Row) -> Character:
    return Character(
        id=row["id"],
        book_id=row["book_id"],
        character_id=row["character_id"],
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


def upsert_book(
    db_path: Path,
    book_id: str,
    title: str,
    context: str = "",
    authors: str = "",
    series: str = "",
    series_index: float | None = None,
    language: str = "",
    description: str = "",
    identifiers: str = "{}",
    keywords: str = "[]",
    total_pages: int | None = None,
    percent_finished: float | None = None,
    reading_status: str = "",
    last_read: str = "",
    highlights: int | None = None,
    notes: int | None = None,
    partial_md5: str = "",
    cover_filename: str = "",
) -> None:
    conn = _connect(db_path)
    conn.execute("""
        INSERT INTO books (
            book_id, title, authors, series, series_index, language, description,
            identifiers, keywords, total_pages, percent_finished, reading_status,
            last_read, highlights, notes, partial_md5, cover_filename, context, uploaded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(book_id) DO UPDATE SET
            title            = excluded.title,
            authors          = excluded.authors,
            series           = excluded.series,
            series_index     = excluded.series_index,
            language         = excluded.language,
            description      = excluded.description,
            identifiers      = excluded.identifiers,
            keywords         = excluded.keywords,
            total_pages      = excluded.total_pages,
            percent_finished = excluded.percent_finished,
            reading_status   = excluded.reading_status,
            last_read        = excluded.last_read,
            highlights       = excluded.highlights,
            notes            = excluded.notes,
            partial_md5      = excluded.partial_md5,
            cover_filename   = excluded.cover_filename,
            context          = excluded.context,
            uploaded_at      = excluded.uploaded_at
    """, (
        book_id, title, authors, series, series_index, language, description,
        identifiers, keywords, total_pages, percent_finished, reading_status,
        last_read, highlights, notes, partial_md5, cover_filename, context,
    ))
    conn.commit()
    conn.close()


def upsert_characters(db_path: Path, book_id: str, characters: list[dict]) -> None:
    conn = _connect(db_path)
    # Delete characters no longer in the upload, then upsert present ones.
    # Use character_id for stable identity; fall back to name if id absent.
    incoming_ids = [
        str(c.get("id") or c.get("name", "")).strip()
        for c in characters
        if (c.get("id") or c.get("name", "")).strip()
    ]
    if incoming_ids:
        placeholders = ",".join("?" * len(incoming_ids))
        conn.execute(
            f"DELETE FROM characters WHERE book_id = ? AND character_id NOT IN ({placeholders})",
            [book_id, *incoming_ids],
        )
    else:
        conn.execute("DELETE FROM characters WHERE book_id = ?", (book_id,))

    for c in characters:
        name = c.get("name", "").strip()
        if not name:
            continue
        character_id = str(c.get("id") or name).strip()
        conn.execute("""
            INSERT INTO characters (
                book_id, character_id, name, aliases, role, occupation,
                physical_description, personality, relationships,
                first_appearance_quote, user_notes, portrait_file,
                source_page, first_seen_page, unlocked, needs_cleanup
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(book_id, character_id) DO UPDATE SET
                name                 = excluded.name,
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
            character_id,
            name,
            json.dumps(c.get("aliases") or []),
            c.get("role") or "unknown",
            c.get("occupation") or "",
            c.get("physical_description") or "",
            c.get("personality") or "",
            json.dumps(c.get("relationships") or []),
            c.get("first_appearance_quote") or "",
            c.get("user_notes") or "",
            Path(c.get("portrait_path") or c.get("portrait_file") or "").name,
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
        WHERE b.deleted_at IS NULL
        GROUP BY b.id
        ORDER BY b.uploaded_at DESC
    """).fetchall()
    conn.close()
    return [_row_to_book(r, r["character_count"]) for r in rows]


def list_deleted_books(db_path: Path) -> list[Book]:
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT b.*, COUNT(c.id) AS character_count
        FROM books b
        LEFT JOIN characters c ON c.book_id = b.book_id
        WHERE b.deleted_at IS NOT NULL
        GROUP BY b.id
        ORDER BY b.deleted_at DESC
    """).fetchall()
    conn.close()
    return [_row_to_book(r, r["character_count"]) for r in rows]


def soft_delete_book(db_path: Path, book_id: str) -> bool:
    conn = _connect(db_path)
    cur = conn.execute(
        "UPDATE books SET deleted_at = datetime('now') WHERE book_id = ? AND deleted_at IS NULL",
        (book_id,),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def restore_book(db_path: Path, book_id: str) -> bool:
    conn = _connect(db_path)
    cur = conn.execute(
        "UPDATE books SET deleted_at = NULL WHERE book_id = ?",
        (book_id,),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def purge_book(db_path: Path, book_id: str) -> bool:
    """Permanently delete a single book from the recycle bin."""
    conn = _connect(db_path)
    cur = conn.execute(
        "DELETE FROM books WHERE book_id = ? AND deleted_at IS NOT NULL",
        (book_id,),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def purge_all_deleted(db_path: Path) -> list[str]:
    """Permanently delete all books in the recycle bin. Returns their book_ids."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT book_id FROM books WHERE deleted_at IS NOT NULL"
    ).fetchall()
    book_ids = [r["book_id"] for r in rows]
    conn.execute("DELETE FROM books WHERE deleted_at IS NOT NULL")
    conn.commit()
    conn.close()
    return book_ids


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


def get_all_characters(db_path: Path) -> dict[str, list[Character]]:
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT * FROM characters
        ORDER BY book_id, first_seen_page ASC NULLS LAST, name ASC
    """).fetchall()
    conn.close()
    result: dict[str, list[Character]] = {}
    for row in rows:
        result.setdefault(row["book_id"], []).append(_row_to_character(row))
    return result


def get_characters(db_path: Path, book_id: str) -> list[Character]:
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT * FROM characters WHERE book_id = ?
        ORDER BY first_seen_page ASC NULLS LAST, name ASC
    """, (book_id,)).fetchall()
    conn.close()
    return [_row_to_character(r) for r in rows]


def get_character(db_path: Path, book_id: str, character_id: str) -> "Character | None":
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM characters WHERE book_id = ? AND character_id = ?",
        (book_id, character_id),
    ).fetchone()
    conn.close()
    return _row_to_character(row) if row else None


def get_book_identifiers(db_path: Path) -> tuple[set[str], set[str]]:
    """Return (titles_lower, partial_md5s) for all non-deleted KoCharacters books."""
    if not db_path.is_file():
        return set(), set()
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT title, partial_md5 FROM books WHERE deleted_at IS NULL"
    ).fetchall()
    conn.close()
    titles = {r["title"].lower().strip() for r in rows if r["title"]}
    md5s = {r["partial_md5"] for r in rows if r["partial_md5"]}
    return titles, md5s


def find_book_id_for_library_book(db_path: Path, title: str, partial_md5: str = "") -> str | None:
    """Return the book_id of a non-deleted KoCharacters book matching md5 or title, or None."""
    if not db_path.is_file():
        return None
    conn = _connect(db_path)
    row = None
    if partial_md5:
        row = conn.execute(
            "SELECT book_id FROM books WHERE partial_md5 = ? AND deleted_at IS NULL LIMIT 1",
            (partial_md5,),
        ).fetchone()
    if not row and title:
        row = conn.execute(
            "SELECT book_id FROM books WHERE title = ? COLLATE NOCASE AND deleted_at IS NULL LIMIT 1",
            (title,),
        ).fetchone()
    conn.close()
    return row["book_id"] if row else None


def delete_book(db_path: Path, book_id: str) -> bool:
    conn = _connect(db_path)
    cur = conn.execute("DELETE FROM books WHERE book_id = ?", (book_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0
