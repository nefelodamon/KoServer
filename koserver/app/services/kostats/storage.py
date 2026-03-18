import hashlib
import sqlite3
from pathlib import Path

from app.services.kostats.models import KoStatsUser


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


async def init_db(db_path: Path) -> None:
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kostats_users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    NOT NULL UNIQUE,
            password_hash TEXT  NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            last_upload TEXT    DEFAULT NULL
        );
    """)
    conn.commit()
    conn.close()


def create_user(db_path: Path, username: str, password: str) -> bool:
    """Returns True if created, False if username taken."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO kostats_users (username, password_hash) VALUES (?, ?)",
            (username, _hash(password)),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def authenticate(db_path: Path, username: str, password: str) -> bool:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM kostats_users WHERE username = ? AND password_hash = ?",
        (username, _hash(password)),
    ).fetchone()
    conn.close()
    return row is not None


def touch_last_upload(db_path: Path, username: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        "UPDATE kostats_users SET last_upload = datetime('now') WHERE username = ?",
        (username,),
    )
    conn.commit()
    conn.close()


def list_users(db_path: Path) -> list[KoStatsUser]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM kostats_users ORDER BY username"
    ).fetchall()
    conn.close()
    return [
        KoStatsUser(
            id=r["id"],
            username=r["username"],
            created_at=r["created_at"],
            last_upload=r["last_upload"],
        )
        for r in rows
    ]


def delete_user(db_path: Path, username: str) -> bool:
    conn = _connect(db_path)
    cur = conn.execute("DELETE FROM kostats_users WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def change_password(db_path: Path, username: str, new_password: str) -> bool:
    conn = _connect(db_path)
    cur = conn.execute(
        "UPDATE kostats_users SET password_hash = ? WHERE username = ?",
        (_hash(new_password), username),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0
