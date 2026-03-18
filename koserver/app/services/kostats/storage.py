import hashlib
import hmac
import os
import sqlite3
from pathlib import Path

from app.services.kostats.models import KoStatsUser

_PBKDF2_ITERATIONS = 260_000


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _hash(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return key.hex(), salt.hex()


def _verify(password: str, stored_hash: str, stored_salt: str) -> bool:
    key, _ = _hash(password, bytes.fromhex(stored_salt))
    return hmac.compare_digest(key, stored_hash)


async def init_db(db_path: Path) -> None:
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kostats_users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL UNIQUE,
            password_hash TEXT    NOT NULL,
            password_salt TEXT    NOT NULL DEFAULT '',
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            last_upload   TEXT    DEFAULT NULL
        );
    """)
    # Migration: add password_salt to existing databases
    try:
        conn.execute("ALTER TABLE kostats_users ADD COLUMN password_salt TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()
    conn.close()


def create_user(db_path: Path, username: str, password: str) -> bool:
    """Returns True if created, False if username taken."""
    hashed, salt = _hash(password)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO kostats_users (username, password_hash, password_salt) VALUES (?, ?, ?)",
            (username, hashed, salt),
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
        "SELECT password_hash, password_salt FROM kostats_users WHERE username = ?",
        (username,),
    ).fetchone()
    conn.close()
    if not row:
        return False
    return _verify(password, row["password_hash"], row["password_salt"])


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
    hashed, salt = _hash(new_password)
    conn = _connect(db_path)
    cur = conn.execute(
        "UPDATE kostats_users SET password_hash = ?, password_salt = ? WHERE username = ?",
        (hashed, salt, username),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0
