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
        "md5": "",
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

    m = re.search(r'\["partial_md5_checksum"\]\s*=\s*"([^"]+)"', content)
    if m:
        result["md5"] = m.group(1)

    # doc_pages as fallback for pages
    if not result["pages"]:
        m = re.search(r'\["doc_pages"\]\s*=\s*([0-9]+)', content)
        if m:
            try:
                result["pages"] = int(m.group(1))
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

            result = await conn.run(
                f'timeout 60 find {device.books_path} -name "*.sdr" -type d 2>/dev/null',
                check=False,
            )
            if result.stderr:
                logger.warning("KoLibrary: find stderr: %s", result.stderr[:300])
            sdr_dirs = [l.strip() for l in result.stdout.splitlines() if l.strip()]
            logger.info("KoLibrary: device %d — found %d .sdr dirs", device_id, len(sdr_dirs))
            total = len(sdr_dirs)
            added = updated = 0

            for idx, sdr_dir in enumerate(sdr_dirs, 1):
                book_path = sdr_dir[:-4]  # strip .sdr
                book_basename = os.path.basename(book_path)

                _status(
                    f"[{idx}/{total}] {book_basename}",
                    added=added, updated=updated,
                )

                # Find metadata lua file via ls (no SFTP needed)
                ls_result = await conn.run(f'ls -1 "{sdr_dir}" 2>/dev/null', check=False)
                lua_names = [
                    n for n in ls_result.stdout.splitlines()
                    if n.endswith(".lua") and not n.endswith(".lua.old")
                ]
                meta_luas = [n for n in lua_names if n.startswith("metadata.")]
                chosen = meta_luas or lua_names
                if not chosen:
                    continue
                lua_file = f"{sdr_dir}/{chosen[0]}"

                # Get mtime via stat
                stat_result = await conn.run(f'stat -c %Y "{lua_file}" 2>/dev/null', check=False)
                try:
                    mtime = int(stat_result.stdout.strip())
                except (ValueError, TypeError):
                    continue

                # Skip if unchanged
                existing = storage.get_book_by_path(db_path, device_id, book_path)
                if existing and existing.file_mtime == mtime:
                    continue

                # Read lua file via cat
                cat_result = await conn.run(f'cat "{lua_file}" 2>/dev/null', check=False)
                if not cat_result.stdout:
                    continue

                try:
                    meta = parse_lua_settings(cat_result.stdout)
                    op = storage.upsert_book(
                        db_path, device_id, book_path, mtime,
                        title=meta["title"] or book_basename,
                        authors=meta["authors"],
                        series=meta["series"],
                        series_index=meta["series_index"],
                        language=meta["language"],
                        pages=meta["pages"],
                        description=meta["description"],
                        cover_file=None,
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

    except (asyncssh.Error, OSError, TimeoutError) as e:
        msg = str(e)
        storage.finish_sync_log(db_path, log_id, "error", 0, 0, msg)
        _status(msg, status="error")
        logger.error("KoLibrary: device %d SSH error: %s", device_id, e)


# Keep placeholder so covers_dir arg stays valid for future use
