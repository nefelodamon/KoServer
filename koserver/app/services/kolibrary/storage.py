import os
import sqlite3
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

from app.services.kolibrary.models import KoBook, KoLibraryDevice, SyncLog


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

def _get_fernet(key_path: Path) -> Fernet:
    if key_path.exists():
        key = key_path.read_bytes()
    else:
        key = Fernet.generate_key()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key)
    return Fernet(key)


def encrypt_password(key_path: Path, password: str) -> str:
    return _get_fernet(key_path).encrypt(password.encode()).decode()


def decrypt_password(key_path: Path, encrypted: str) -> str:
    return _get_fernet(key_path).decrypt(encrypted.encode()).decode()


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

async def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kolibrary_devices (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            name              TEXT NOT NULL,
            friendly_name     TEXT NOT NULL DEFAULT '',
            host              TEXT NOT NULL,
            port              INTEGER NOT NULL DEFAULT 22,
            username          TEXT NOT NULL,
            encrypted_password TEXT NOT NULL DEFAULT '',
            books_path        TEXT NOT NULL DEFAULT '/mnt/us/documents',
            sync_interval     TEXT NOT NULL DEFAULT 'daily',
            last_sync         TEXT DEFAULT NULL,
            created_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS kolibrary_sync_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id    INTEGER NOT NULL REFERENCES kolibrary_devices(id) ON DELETE CASCADE,
            started_at   TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at  TEXT DEFAULT NULL,
            status       TEXT NOT NULL DEFAULT 'running',
            books_added  INTEGER NOT NULL DEFAULT 0,
            books_updated INTEGER NOT NULL DEFAULT 0,
            message      TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS kolibrary_books (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id      INTEGER NOT NULL REFERENCES kolibrary_devices(id) ON DELETE CASCADE,
            file_path      TEXT NOT NULL,
            file_mtime     INTEGER NOT NULL DEFAULT 0,
            md5            TEXT DEFAULT NULL,
            title          TEXT NOT NULL DEFAULT '',
            authors        TEXT NOT NULL DEFAULT '',
            series         TEXT NOT NULL DEFAULT '',
            series_index   REAL DEFAULT NULL,
            language       TEXT NOT NULL DEFAULT '',
            pages          INTEGER NOT NULL DEFAULT 0,
            description    TEXT NOT NULL DEFAULT '',
            cover_file     TEXT DEFAULT NULL,
            progress_pct   REAL NOT NULL DEFAULT 0.0,
            status         TEXT NOT NULL DEFAULT '',
            last_synced_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(device_id, file_path)
        );
    """)
    conn.commit()
    # Migration: add md5 column to existing databases
    try:
        conn.execute("ALTER TABLE kolibrary_books ADD COLUMN md5 TEXT DEFAULT NULL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE kolibrary_books ADD COLUMN status TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

def list_devices(db_path: Path) -> list[KoLibraryDevice]:
    conn = _connect(db_path)
    rows = conn.execute("SELECT * FROM kolibrary_devices ORDER BY name").fetchall()
    conn.close()
    return [_row_to_device(r) for r in rows]


def get_device(db_path: Path, device_id: int) -> Optional[KoLibraryDevice]:
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM kolibrary_devices WHERE id = ?", (device_id,)).fetchone()
    conn.close()
    return _row_to_device(row) if row else None


def create_device(db_path: Path, name: str, friendly_name: str, host: str, port: int,
                  username: str, encrypted_password: str, books_path: str, sync_interval: str) -> int:
    conn = _connect(db_path)
    cur = conn.execute(
        "INSERT INTO kolibrary_devices (name, friendly_name, host, port, username, encrypted_password, books_path, sync_interval) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, friendly_name, host, port, username, encrypted_password, books_path, sync_interval),
    )
    conn.commit()
    device_id = cur.lastrowid
    conn.close()
    return device_id


def update_device(db_path: Path, device_id: int, name: str, friendly_name: str, host: str,
                  port: int, username: str, books_path: str, sync_interval: str,
                  encrypted_password: Optional[str] = None) -> None:
    conn = _connect(db_path)
    if encrypted_password is not None:
        conn.execute(
            "UPDATE kolibrary_devices SET name=?, friendly_name=?, host=?, port=?, username=?, "
            "books_path=?, sync_interval=?, encrypted_password=? WHERE id=?",
            (name, friendly_name, host, port, username, books_path, sync_interval, encrypted_password, device_id),
        )
    else:
        conn.execute(
            "UPDATE kolibrary_devices SET name=?, friendly_name=?, host=?, port=?, username=?, "
            "books_path=?, sync_interval=? WHERE id=?",
            (name, friendly_name, host, port, username, books_path, sync_interval, device_id),
        )
    conn.commit()
    conn.close()


def delete_device(db_path: Path, device_id: int) -> None:
    conn = _connect(db_path)
    conn.execute("DELETE FROM kolibrary_devices WHERE id = ?", (device_id,))
    conn.commit()
    conn.close()


def update_device_last_sync(db_path: Path, device_id: int) -> None:
    conn = _connect(db_path)
    conn.execute("UPDATE kolibrary_devices SET last_sync = datetime('now') WHERE id = ?", (device_id,))
    conn.commit()
    conn.close()


def _row_to_device(r) -> KoLibraryDevice:
    return KoLibraryDevice(
        id=r["id"], name=r["name"], friendly_name=r["friendly_name"] or "",
        host=r["host"], port=r["port"], username=r["username"],
        encrypted_password=r["encrypted_password"] or "",
        books_path=r["books_path"], sync_interval=r["sync_interval"],
        last_sync=r["last_sync"], created_at=r["created_at"],
    )


# ---------------------------------------------------------------------------
# Sync log
# ---------------------------------------------------------------------------

def start_sync_log(db_path: Path, device_id: int) -> int:
    conn = _connect(db_path)
    cur = conn.execute(
        "INSERT INTO kolibrary_sync_log (device_id, status) VALUES (?, 'running')", (device_id,)
    )
    conn.commit()
    log_id = cur.lastrowid
    conn.close()
    return log_id


def finish_sync_log(db_path: Path, log_id: int, status: str,
                    books_added: int, books_updated: int, message: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        "UPDATE kolibrary_sync_log SET finished_at=datetime('now'), status=?, "
        "books_added=?, books_updated=?, message=? WHERE id=?",
        (status, books_added, books_updated, message, log_id),
    )
    conn.commit()
    conn.close()


def clear_sync_logs(db_path: Path, device_id: int) -> None:
    conn = _connect(db_path)
    conn.execute("DELETE FROM kolibrary_sync_log WHERE device_id = ?", (device_id,))
    conn.commit()
    conn.close()


def mark_stale_running_logs(db_path: Path) -> None:
    """On startup, any log still 'running' was interrupted by a restart."""
    conn = _connect(db_path)
    conn.execute(
        "UPDATE kolibrary_sync_log SET status='interrupted', finished_at=datetime('now'), "
        "message='Interrupted by server restart' WHERE status='running'"
    )
    conn.commit()
    conn.close()


def list_sync_logs(db_path: Path, device_id: int, limit: int = 20) -> list[SyncLog]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM kolibrary_sync_log WHERE device_id=? ORDER BY started_at DESC LIMIT ?",
        (device_id, limit),
    ).fetchall()
    conn.close()
    return [SyncLog(
        id=r["id"], device_id=r["device_id"], started_at=r["started_at"],
        finished_at=r["finished_at"], status=r["status"],
        books_added=r["books_added"], books_updated=r["books_updated"], message=r["message"],
    ) for r in rows]


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------

def get_book_by_path(db_path: Path, device_id: int, file_path: str) -> Optional[KoBook]:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM kolibrary_books WHERE device_id=? AND file_path=?", (device_id, file_path)
    ).fetchone()
    conn.close()
    return _row_to_book(row, "") if row else None


def upsert_book(db_path: Path, device_id: int, file_path: str, file_mtime: int,
                title: str, authors: str, series: str, series_index: Optional[float],
                language: str, pages: int, description: str,
                cover_file: Optional[str], progress_pct: float,
                md5: Optional[str] = None, status: str = "") -> str:
    """Returns 'added' or 'updated'."""
    conn = _connect(db_path)
    existing = conn.execute(
        "SELECT id FROM kolibrary_books WHERE device_id=? AND file_path=?", (device_id, file_path)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE kolibrary_books SET file_mtime=?, md5=?, title=?, authors=?, series=?, series_index=?, "
            "language=?, pages=?, description=?, cover_file=?, progress_pct=?, status=?, last_synced_at=datetime('now') "
            "WHERE device_id=? AND file_path=?",
            (file_mtime, md5, title, authors, series, series_index, language, pages, description,
             cover_file, progress_pct, status, device_id, file_path),
        )
        result = "updated"
    else:
        conn.execute(
            "INSERT INTO kolibrary_books (device_id, file_path, file_mtime, md5, title, authors, series, "
            "series_index, language, pages, description, cover_file, progress_pct, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, file_path, file_mtime, md5, title, authors, series, series_index,
             language, pages, description, cover_file, progress_pct, status),
        )
        result = "added"
    conn.commit()
    conn.close()
    return result


def load_kosync_progress(kosync_db_path: Path) -> dict[str, float]:
    """Return {md5: percentage} from kosync_progress for all users."""
    if not kosync_db_path.is_file():
        return {}
    try:
        conn = _connect(kosync_db_path)
        rows = conn.execute(
            "SELECT document, MAX(percentage) as pct FROM kosync_progress GROUP BY document"
        ).fetchall()
        conn.close()
        return {r["document"]: r["pct"] for r in rows if r["document"]}
    except Exception:
        return {}


def delete_device_books(db_path: Path, device_id: int) -> None:
    conn = _connect(db_path)
    conn.execute("DELETE FROM kolibrary_books WHERE device_id = ?", (device_id,))
    conn.commit()
    conn.close()


def delete_all_books(db_path: Path) -> None:
    conn = _connect(db_path)
    conn.execute("DELETE FROM kolibrary_books")
    conn.commit()
    conn.close()


def list_books(db_path: Path, device_id: Optional[int] = None,
               search: str = "", status_filter: str = "") -> list[KoBook]:
    conn = _connect(db_path)
    query = """
        SELECT b.*, d.name, d.friendly_name
        FROM kolibrary_books b
        JOIN kolibrary_devices d ON d.id = b.device_id
        WHERE 1=1
    """
    args: list = []
    if device_id is not None:
        query += " AND b.device_id = ?"
        args.append(device_id)
    if search:
        query += " AND (b.title LIKE ? OR b.authors LIKE ?)"
        args += [f"%{search}%", f"%{search}%"]
    if status_filter in ("reading", "complete", "abandoned", "tbr"):
        query += " AND b.status = ?"
        args.append(status_filter)
    elif status_filter == "unread":
        query += " AND b.status = '' AND b.progress_pct = 0"
    query += " ORDER BY b.title COLLATE NOCASE"
    rows = conn.execute(query, args).fetchall()
    conn.close()
    return [_row_to_book(r, r["friendly_name"] or r["name"]) for r in rows]


def _row_to_book(r, device_display_name: str) -> KoBook:
    return KoBook(
        id=r["id"], device_id=r["device_id"], device_display_name=device_display_name,
        file_path=r["file_path"], file_mtime=r["file_mtime"],
        md5=r["md5"] if "md5" in r.keys() else None,
        title=r["title"] or "", authors=r["authors"] or "",
        series=r["series"] or "", series_index=r["series_index"],
        language=r["language"] or "", pages=r["pages"] or 0,
        description=r["description"] or "",
        cover_file=r["cover_file"],
        progress_pct=r["progress_pct"] or 0.0,
        status=r["status"] if "status" in r.keys() else "",
        last_synced_at=r["last_synced_at"],
    )
