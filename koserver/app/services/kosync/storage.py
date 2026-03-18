import sqlite3
from pathlib import Path

from app.services.kosync.models import KoSyncUser, ReadingProgress

ALLOW_REGISTRATION_KEY = "allow_registration"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


async def init_db(db_path: Path) -> None:
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kosync_users (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            username     TEXT    NOT NULL UNIQUE,
            password_hash TEXT   NOT NULL,
            created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            last_sync    TEXT    DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS kosync_progress (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    NOT NULL REFERENCES kosync_users(username) ON DELETE CASCADE,
            document    TEXT    NOT NULL,
            progress    TEXT    NOT NULL DEFAULT '',
            percentage  REAL    NOT NULL DEFAULT 0,
            device      TEXT    NOT NULL DEFAULT '',
            device_id   TEXT    NOT NULL DEFAULT '',
            timestamp   INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(username, document)
        );

        CREATE TABLE IF NOT EXISTS kosync_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.execute(
        f"INSERT OR IGNORE INTO kosync_settings (key, value) VALUES ('{ALLOW_REGISTRATION_KEY}', 'true')"
    )
    conn.commit()
    conn.close()


def get_setting(db_path: Path, key: str, default: str = "") -> str:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT value FROM kosync_settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(db_path: Path, key: str, value: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO kosync_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def create_user(db_path: Path, username: str, password_hash: str) -> bool:
    """Returns True if created, False if username already taken."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO kosync_users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def authenticate(db_path: Path, username: str, password_hash: str) -> bool:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM kosync_users WHERE username = ? AND password_hash = ?",
        (username, password_hash),
    ).fetchone()
    conn.close()
    return row is not None


def upsert_progress(
    db_path: Path,
    username: str,
    document: str,
    progress: str,
    percentage: float,
    device: str,
    device_id: str,
    timestamp: int,
) -> None:
    conn = _connect(db_path)
    conn.execute(
        """
        INSERT INTO kosync_progress
            (username, document, progress, percentage, device, device_id, timestamp, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(username, document) DO UPDATE SET
            progress   = excluded.progress,
            percentage = excluded.percentage,
            device     = excluded.device,
            device_id  = excluded.device_id,
            timestamp  = excluded.timestamp,
            updated_at = excluded.updated_at
        """,
        (username, document, progress, percentage, device, device_id, timestamp),
    )
    conn.execute(
        "UPDATE kosync_users SET last_sync = datetime('now') WHERE username = ?",
        (username,),
    )
    conn.commit()
    conn.close()


def get_progress(db_path: Path, username: str, document: str) -> ReadingProgress | None:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM kosync_progress WHERE username = ? AND document = ?",
        (username, document),
    ).fetchone()
    conn.close()
    return _row_to_progress(row) if row else None


def _row_to_progress(row: sqlite3.Row) -> ReadingProgress:
    return ReadingProgress(
        id=row["id"],
        username=row["username"],
        document=row["document"],
        progress=row["progress"],
        percentage=row["percentage"],
        device=row["device"],
        device_id=row["device_id"],
        timestamp=row["timestamp"],
        updated_at=row["updated_at"],
    )


def list_all_progress(db_path: Path) -> list[ReadingProgress]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM kosync_progress ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [_row_to_progress(r) for r in rows]


def get_progress_by_document(db_path: Path, document: str) -> list[ReadingProgress]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM kosync_progress WHERE document = ? ORDER BY updated_at DESC",
        (document,),
    ).fetchall()
    conn.close()
    return [_row_to_progress(r) for r in rows]


def list_user_progress(db_path: Path, username: str) -> list[ReadingProgress]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM kosync_progress WHERE username = ? ORDER BY updated_at DESC",
        (username,),
    ).fetchall()
    conn.close()
    return [_row_to_progress(r) for r in rows]


def list_users(db_path: Path) -> list[KoSyncUser]:
    conn = _connect(db_path)
    rows = conn.execute(
        """
        SELECT u.id, u.username, u.created_at, u.last_sync, COUNT(p.id) AS document_count
        FROM kosync_users u
        LEFT JOIN kosync_progress p ON p.username = u.username
        GROUP BY u.id
        ORDER BY u.username
        """
    ).fetchall()
    conn.close()
    return [
        KoSyncUser(
            id=r["id"],
            username=r["username"],
            created_at=r["created_at"],
            last_sync=r["last_sync"],
            document_count=r["document_count"],
        )
        for r in rows
    ]


def delete_user(db_path: Path, username: str) -> bool:
    conn = _connect(db_path)
    cur = conn.execute("DELETE FROM kosync_users WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0
