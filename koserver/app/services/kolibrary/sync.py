import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Optional

import asyncssh

from app.services.kolibrary import storage
from app.services.kolibrary.models import KoLibraryDevice

logger = logging.getLogger(__name__)

# In-memory live status per device_id
_sync_status: dict[int, dict] = {}
_sync_locks: dict[int, asyncio.Lock] = {}


def get_sync_status(device_id: int) -> dict:
    return _sync_status.get(device_id, {"status": "idle", "message": "", "books_added": 0, "books_updated": 0})


def _lock_for(device_id: int) -> asyncio.Lock:
    if device_id not in _sync_locks:
        _sync_locks[device_id] = asyncio.Lock()
    return _sync_locks[device_id]


# ---------------------------------------------------------------------------
# Lua parser
# ---------------------------------------------------------------------------

def _parse_lua_scalar(s: str):
    s = s.strip().rstrip(",").strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1].replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\").replace("\\n", "\n")
    if s == "true":
        return True
    if s == "false":
        return False
    if s == "nil":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _extract_nested_table(text: str, key: str) -> Optional[str]:
    m = re.search(rf'\["{re.escape(key)}"\]\s*=\s*\{{', text)
    if not m:
        return None
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start : i - 1]


def _parse_flat_table(text: str) -> dict:
    result = {}
    for m in re.finditer(
        r'\["([^"]+)"\]\s*=\s*((?:"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^,\n{}\[\]]+))', text
    ):
        key = m.group(1)
        val = m.group(2).strip().rstrip(",").strip()
        result[key] = _parse_lua_scalar(val)
    return result


def parse_lua_settings(content: str) -> dict:
    """Extract doc_props and percent_finished from a KOReader .lua settings file."""
    result: dict = {
        "title": "", "authors": "", "series": "", "series_index": None,
        "language": "", "pages": 0, "description": "", "percent_finished": 0.0,
    }
    doc_props_text = _extract_nested_table(content, "doc_props")
    if doc_props_text:
        dp = _parse_flat_table(doc_props_text)
        for field in ("title", "authors", "series", "language", "description"):
            if field in dp and isinstance(dp[field], str):
                result[field] = dp[field]
        if "series_index" in dp:
            try:
                result["series_index"] = float(dp["series_index"])
            except (TypeError, ValueError):
                pass
        if "pages" in dp:
            try:
                result["pages"] = int(dp["pages"])
            except (TypeError, ValueError):
                pass

    m = re.search(r'\["percent_finished"\]\s*=\s*([0-9.]+)', content)
    if m:
        try:
            result["percent_finished"] = min(1.0, float(m.group(1)))
        except ValueError:
            pass
    return result


# ---------------------------------------------------------------------------
# SSH sync
# ---------------------------------------------------------------------------

async def sync_device(
    device_id: int,
    db_path: Path,
    covers_dir: Path,
    key_path: Path,
) -> None:
    lock = _lock_for(device_id)
    if lock.locked():
        logger.info("KoLibrary: sync already running for device %d, skipping", device_id)
        return

    async with lock:
        await _run_sync(device_id, db_path, covers_dir, key_path)


async def _run_sync(device_id: int, db_path: Path, covers_dir: Path, key_path: Path) -> None:
    device = storage.get_device(db_path, device_id)
    if not device:
        return

    def _status(msg: str, added: int = 0, updated: int = 0, status: str = "running"):
        _sync_status[device_id] = {
            "status": status, "message": msg,
            "books_added": added, "books_updated": updated,
        }

    _status("Connecting…")
    log_id = storage.start_sync_log(db_path, device_id)

    try:
        password = storage.decrypt_password(key_path, device.encrypted_password)
    except Exception as e:
        msg = f"Failed to decrypt password: {e}"
        storage.finish_sync_log(db_path, log_id, "error", 0, 0, msg)
        _status(msg, status="error")
        return

    try:
        async with asyncssh.connect(
            device.host,
            port=device.port,
            username=device.username,
            password=password,
            known_hosts=None,
            connect_timeout=15,
        ) as conn:
            _status("Finding books…")

            result = await asyncio.wait_for(
                conn.run(
                    f'find {device.books_path} -name "*.sdr" -type d 2>/dev/null',
                    check=False,
                ),
                timeout=60,
            )
            if result.returncode not in (0, 1) or (not result.stdout and result.stderr):
                logger.warning("KoLibrary: find stderr: %s", result.stderr)
            sdr_dirs = [l.strip() for l in result.stdout.splitlines() if l.strip()]
            logger.info("KoLibrary: find returned %d dirs, stderr=%r", len(sdr_dirs), result.stderr[:200] if result.stderr else "")
            total = len(sdr_dirs)
            logger.info("KoLibrary: device %d — found %d .sdr dirs", device_id, total)

            added = updated = 0

            async with conn.start_sftp_client() as sftp:
                for idx, sdr_dir in enumerate(sdr_dirs, 1):
                    book_path = sdr_dir[:-4]  # strip .sdr
                    book_basename = os.path.basename(book_path)
                    lua_file = f"{sdr_dir}/{book_basename}.lua"

                    _status(
                        f"[{idx}/{total}] {book_basename}",
                        books_added=added, books_updated=updated,
                    )

                    try:
                        stat = await sftp.stat(lua_file)
                        mtime = int(stat.mtime)
                    except Exception:
                        continue

                    # Skip if unchanged
                    existing = storage.get_book_by_path(db_path, device_id, book_path)
                    if existing and existing.file_mtime == mtime:
                        continue

                    try:
                        async with await sftp.open(lua_file, "r") as f:
                            lua_content = await f.read()
                        meta = parse_lua_settings(lua_content)

                        # Skip nameless/empty entries
                        if not meta["title"] and not book_basename:
                            continue

                        # Try to download cover image
                        cover_file = await _download_cover(
                            sftp, sdr_dir, device_id, book_basename, covers_dir
                        )

                        op = storage.upsert_book(
                            db_path, device_id, book_path, mtime,
                            title=meta["title"] or book_basename,
                            authors=meta["authors"],
                            series=meta["series"],
                            series_index=meta["series_index"],
                            language=meta["language"],
                            pages=meta["pages"],
                            description=meta["description"],
                            cover_file=cover_file,
                            progress_pct=meta["percent_finished"],
                        )
                        if op == "added":
                            added += 1
                        else:
                            updated += 1

                    except Exception as e:
                        logger.warning("KoLibrary: failed to process %s: %s", lua_file, e)

            storage.update_device_last_sync(db_path, device_id)
            msg = f"Done: {added} added, {updated} updated"
            storage.finish_sync_log(db_path, log_id, "success", added, updated, msg)
            _status(msg, added, updated, status="success")
            logger.info("KoLibrary: device %d sync complete — %s", device_id, msg)

    except asyncio.TimeoutError:
        msg = "Timed out — check that the books path exists and is accessible"
        storage.finish_sync_log(db_path, log_id, "error", 0, 0, msg)
        _status(msg, status="error")
        logger.error("KoLibrary: device %d timed out during find", device_id)
    except (asyncssh.Error, OSError, TimeoutError) as e:
        msg = str(e)
        storage.finish_sync_log(db_path, log_id, "error", 0, 0, msg)
        _status(msg, status="error")
        logger.error("KoLibrary: device %d SSH error: %s", device_id, e)


async def _download_cover(
    sftp, sdr_dir: str, device_id: int, book_basename: str, covers_dir: Path
) -> Optional[str]:
    """Try to find and download a cover image from the .sdr directory."""
    try:
        entries = await sftp.readdir(sdr_dir)
        image_names = sorted(
            [e.filename for e in entries if re.search(r"\.(jpg|jpeg|png)$", e.filename, re.I)],
            # prefer thumbnail_ files (CoverBrowser), then cover.*, then anything
            key=lambda n: (0 if n.startswith("thumbnail_") else 1 if "cover" in n.lower() else 2, n),
        )
        if not image_names:
            return None

        src = f"{sdr_dir}/{image_names[0]}"
        dest_dir = covers_dir / str(device_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Use a stable filename based on book basename
        safe_name = re.sub(r"[^\w\-.]", "_", book_basename) + ".jpg"
        dest = dest_dir / safe_name
        await sftp.get(src, str(dest))
        return f"{device_id}/{safe_name}"
    except Exception:
        return None
