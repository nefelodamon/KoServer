import asyncio
import base64
import hashlib
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
# Strong references to running tasks so the GC doesn't collect them (Python 3.12+)
_running_tasks: set = set()


def create_sync_task(coro) -> asyncio.Task:
    """Create a fire-and-forget sync task and hold a strong reference until it completes."""
    task = asyncio.create_task(coro)
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)
    return task


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
        "md5": "", "status": "",
    }
    doc_props_text = _extract_nested_table(content, "doc_props")
    if doc_props_text:
        dp = _parse_flat_table(doc_props_text)
        for field in ("title", "authors", "series", "language", "description"):
            if field in dp and isinstance(dp[field], str):
                val = dp[field]
                if field == "description":
                    val = re.sub(r'<[^>]+>', '', val).strip()
                result[field] = val
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

    summary_text = _extract_nested_table(content, "summary")
    if summary_text:
        sp = _parse_flat_table(summary_text)
        result["status"] = sp.get("status", "") or ""

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
# Cover fetcher
# ---------------------------------------------------------------------------

def _parse_container_xml(content: str) -> Optional[str]:
    """Extract OPF path from META-INF/container.xml."""
    m = re.search(r'full-path\s*=\s*["\']([^"\']+\.opf)["\']', content, re.IGNORECASE)
    return m.group(1) if m else None


def _parse_opf_cover(content: str, opf_path: str) -> Optional[str]:
    """Return the cover image path relative to the ZIP root, or None."""
    opf_dir = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""

    def resolve(href: str) -> str:
        return f"{opf_dir}/{href}" if opf_dir else href

    # Match <item> or <opf:item> (namespace-prefixed variants)
    item_pat = r'<(?:\w+:)?item\b'

    # EPUB3: <item properties="cover-image" href="..."/>
    m = re.search(item_pat + r'[^>]+\bproperties=["\']cover-image["\'][^>]+\bhref=["\']([^"\']+)["\']', content, re.IGNORECASE)
    if not m:
        m = re.search(item_pat + r'[^>]+\bhref=["\']([^"\']+)["\'][^>]+\bproperties=["\']cover-image["\']', content, re.IGNORECASE)
    if m:
        return resolve(m.group(1))

    # EPUB2: <meta name="cover" content="cover-id-or-path"/>
    m = re.search(r'<(?:\w+:)?meta\b[^>]+\bname=["\']cover["\'][^>]+\bcontent=["\']([^"\']+)["\']', content, re.IGNORECASE)
    if not m:
        m = re.search(r'<(?:\w+:)?meta\b[^>]+\bcontent=["\']([^"\']+)["\'][^>]+\bname=["\']cover["\']', content, re.IGNORECASE)
    if m:
        cover_val = m.group(1)
        # Try as item ID
        eid = re.escape(cover_val)
        m2 = re.search(item_pat + r'[^>]+\bid=["\']' + eid + r'["\'][^>]+\bhref=["\']([^"\']+)["\']', content, re.IGNORECASE)
        if not m2:
            m2 = re.search(item_pat + r'[^>]+\bhref=["\']([^"\']+)["\'][^>]+\bid=["\']' + eid + r'["\']', content, re.IGNORECASE)
        if m2:
            return resolve(m2.group(1))
        # content is a direct image path
        if re.search(r'\.(jpe?g|png|gif|webp)$', cover_val, re.IGNORECASE):
            return resolve(cover_val)

    # Last resort: find any manifest item whose href contains "cover" and is an image
    m = re.search(item_pat + r'[^>]+\bhref=["\']([^"\']*cover[^"\']*\.(?:jpe?g|png|gif|webp))["\']', content, re.IGNORECASE)
    if m:
        return resolve(m.group(1))

    return None


def _parse_opf_metadata(opf_content: str) -> dict:
    """Extract Dublin Core + Calibre metadata from OPF file content."""
    result: dict = {
        "title": "", "authors": "", "series": "", "series_index": None,
        "language": "", "pages": 0, "description": "",
    }

    m = re.search(r'<(?:\w+:)?title\b[^>]*>([^<]+)</(?:\w+:)?title>', opf_content, re.IGNORECASE)
    if m:
        result["title"] = m.group(1).strip()

    creators = re.findall(r'<(?:\w+:)?creator\b[^>]*>([^<]+)</(?:\w+:)?creator>', opf_content, re.IGNORECASE)
    if creators:
        result["authors"] = ", ".join(a.strip() for a in creators)

    m = re.search(r'<(?:\w+:)?description\b[^>]*>([\s\S]*?)</(?:\w+:)?description>', opf_content, re.IGNORECASE)
    if m:
        result["description"] = re.sub(r'<[^>]+>', '', m.group(1)).strip()

    m = re.search(r'<(?:\w+:)?language\b[^>]*>([^<]+)</(?:\w+:)?language>', opf_content, re.IGNORECASE)
    if m:
        result["language"] = m.group(1).strip()

    # Calibre series
    m = re.search(r'<meta\b[^>]+\bname=["\']calibre:series["\'][^>]+\bcontent=["\']([^"\']+)["\']', opf_content, re.IGNORECASE)
    if not m:
        m = re.search(r'<meta\b[^>]+\bcontent=["\']([^"\']+)["\'][^>]+\bname=["\']calibre:series["\']', opf_content, re.IGNORECASE)
    if m:
        result["series"] = m.group(1).strip()

    # EPUB3 belongs-to-collection fallback
    if not result["series"]:
        m = re.search(r'<meta\b[^>]+\bproperty=["\']belongs-to-collection["\'][^>]*>([^<]+)</meta>', opf_content, re.IGNORECASE)
        if m:
            result["series"] = m.group(1).strip()

    # Calibre series index
    m = re.search(r'<meta\b[^>]+\bname=["\']calibre:series_index["\'][^>]+\bcontent=["\']([^"\']+)["\']', opf_content, re.IGNORECASE)
    if not m:
        m = re.search(r'<meta\b[^>]+\bcontent=["\']([^"\']+)["\'][^>]+\bname=["\']calibre:series_index["\']', opf_content, re.IGNORECASE)
    if m:
        try:
            result["series_index"] = float(m.group(1).strip())
        except ValueError:
            pass

    return result


async def _fetch_epub_meta_and_cover(
    conn,
    epub_path: str,
    covers_dir: Path,
    device_id: int,
    existing_cover: Optional[str],
) -> tuple[dict, Optional[str]]:
    """Extract metadata and cover from EPUB via SSH in a single OPF read pass."""
    empty: dict = {
        "title": "", "authors": "", "series": "", "series_index": None,
        "language": "", "pages": 0, "description": "",
    }

    r = await conn.run(f'unzip -p "{epub_path}" META-INF/container.xml 2>/dev/null', check=False)
    if not r.stdout:
        return empty, existing_cover
    opf_path = _parse_container_xml(r.stdout)
    if not opf_path:
        return empty, existing_cover

    r = await conn.run(f'unzip -p "{epub_path}" "{opf_path}" 2>/dev/null', check=False)
    if not r.stdout:
        return empty, existing_cover
    opf_content = r.stdout

    meta = _parse_opf_metadata(opf_content)

    cover_file = existing_cover
    if cover_file is None:
        cover_zip_path = _parse_opf_cover(opf_content, opf_path)
        if cover_zip_path:
            r = await conn.run(f'unzip -p "{epub_path}" "{cover_zip_path}" 2>/dev/null | base64', check=False)
            if r.stdout and r.stdout.strip():
                try:
                    image_bytes = base64.b64decode(r.stdout.strip())
                    if image_bytes:
                        ext = cover_zip_path.rsplit(".", 1)[-1].lower() if "." in cover_zip_path else "jpg"
                        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
                            ext = "jpg"
                        cover_name = hashlib.md5(f"{device_id}:{epub_path}".encode()).hexdigest() + f".{ext}"
                        cover_subdir = covers_dir / str(device_id)
                        cover_subdir.mkdir(parents=True, exist_ok=True)
                        (cover_subdir / cover_name).write_bytes(image_bytes)
                        cover_file = f"{device_id}/{cover_name}"
                        logger.info("KoLibrary: saved epub cover %s (%d bytes)", cover_name, len(image_bytes))
                except Exception as e:
                    logger.warning("KoLibrary: cover decode failed for %s: %s", epub_path, e)

    return meta, cover_file


async def _fetch_cover(conn, book_path: str, covers_dir: Path, device_id: int) -> Optional[str]:
    """Fetch cover image from an EPUB via SSH. Returns relative path or None."""
    # book_path has no extension (sdr dirs on this device omit it); try .epub
    if book_path.lower().endswith(".epub"):
        epub_path = book_path
    else:
        epub_path = book_path + ".epub"
    try:
        # Step 1: container.xml → OPF path
        r = await conn.run(f'unzip -p "{epub_path}" META-INF/container.xml 2>/dev/null', check=False)
        if not r.stdout:
            logger.warning("KoLibrary: cover step1 empty for %s", epub_path)
            return None
        opf_path = _parse_container_xml(r.stdout)
        if not opf_path:
            logger.warning("KoLibrary: cover step1 no OPF path in container.xml for %s", epub_path)
            return None
        logger.debug("KoLibrary: cover step1 OPF=%s for %s", opf_path, epub_path)

        # Step 2: OPF → cover image zip path
        r = await conn.run(f'unzip -p "{epub_path}" "{opf_path}" 2>/dev/null', check=False)
        if not r.stdout:
            logger.warning("KoLibrary: cover step2 empty OPF for %s", epub_path)
            return None
        cover_zip_path = _parse_opf_cover(r.stdout, opf_path)
        if not cover_zip_path:
            logger.warning("KoLibrary: cover step2 no cover href in OPF for %s", epub_path)
            return None
        logger.debug("KoLibrary: cover step2 cover_zip_path=%s for %s", cover_zip_path, epub_path)

        # Step 3: extract cover image as base64 to avoid binary corruption over SSH
        r = await conn.run(f'unzip -p "{epub_path}" "{cover_zip_path}" 2>/dev/null | base64', check=False)
        if not r.stdout or not r.stdout.strip():
            logger.warning("KoLibrary: cover step3 empty base64 for %s (cover_zip_path=%s)", epub_path, cover_zip_path)
            return None
        image_bytes = base64.b64decode(r.stdout.strip())
        if not image_bytes:
            logger.warning("KoLibrary: cover step3 zero bytes after decode for %s", epub_path)
            return None

        ext = cover_zip_path.rsplit(".", 1)[-1].lower() if "." in cover_zip_path else "jpg"
        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            ext = "jpg"

        cover_name = hashlib.md5(f"{device_id}:{book_path}".encode()).hexdigest() + f".{ext}"
        cover_subdir = covers_dir / str(device_id)
        cover_subdir.mkdir(parents=True, exist_ok=True)
        (cover_subdir / cover_name).write_bytes(image_bytes)
        logger.info("KoLibrary: saved cover %s (%d bytes)", cover_name, len(image_bytes))
        return f"{device_id}/{cover_name}"

    except Exception as e:
        logger.warning("KoLibrary: cover fetch failed for %s: %s", epub_path, e, exc_info=True)
        return None


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

                # A Phase 2 record may have stored this book under the .epub path;
                # find it either way so we update in-place rather than duplicate
                existing = storage.get_book_by_path(db_path, device_id, book_path)
                if existing is None and not book_path.lower().endswith(".epub"):
                    existing = storage.get_book_by_path(db_path, device_id, book_path + ".epub")
                # Reuse whichever path is already in the DB to avoid creating a duplicate row
                effective_path = existing.file_path if existing else book_path

                if existing and existing.file_mtime == mtime and existing.cover_file:
                    continue

                # Read lua file via cat
                cat_result = await conn.run(f'cat "{lua_file}" 2>/dev/null', check=False)
                if not cat_result.stdout:
                    continue

                try:
                    meta = parse_lua_settings(cat_result.stdout)

                    # Fetch cover if not already stored
                    cover_file = existing.cover_file if existing else None
                    if cover_file is None:
                        logger.info("KoLibrary: fetching cover for %s", book_path)
                        cover_file = await _fetch_cover(conn, book_path, covers_dir, device_id)
                    else:
                        logger.debug("KoLibrary: cover already stored for %s", book_path)

                    op = storage.upsert_book(
                        db_path, device_id, effective_path, mtime,
                        title=meta["title"] or book_basename,
                        authors=meta["authors"],
                        series=meta["series"],
                        series_index=meta["series_index"],
                        language=meta["language"],
                        pages=meta["pages"],
                        description=meta["description"],
                        cover_file=cover_file,
                        progress_pct=meta["percent_finished"],
                        md5=meta.get("md5") or None,
                        status=meta.get("status", ""),
                    )
                    if op == "added":
                        added += 1
                    else:
                        updated += 1
                except Exception as e:
                    logger.warning("KoLibrary: failed to process %s: %s", lua_file, e)

            # ------------------------------------------------------------------
            # Phase 2: scan for EPUB files that have no .sdr directory
            # ------------------------------------------------------------------
            _status("Scanning for untracked EPUBs…", added=added, updated=updated)
            epub_result = await conn.run(
                f'timeout 60 find {device.books_path} -name "*.epub" -type f 2>/dev/null',
                check=False,
            )
            epub_paths = [l.strip() for l in epub_result.stdout.splitlines() if l.strip()]
            # sdr dirs may be named BookName.sdr OR BookName.epub.sdr depending on device;
            # include both forms so the filter works either way
            sdr_book_paths: set[str] = set()
            for _sdr in sdr_dirs:
                _bp = _sdr[:-4]
                sdr_book_paths.add(_bp)
                if not _bp.lower().endswith(".epub"):
                    sdr_book_paths.add(_bp + ".epub")
            untracked_epubs = [p for p in epub_paths if p not in sdr_book_paths]
            logger.info(
                "KoLibrary: device %d — %d EPUBs total, %d untracked",
                device_id, len(epub_paths), len(untracked_epubs),
            )

            epub_total = len(untracked_epubs)
            for idx, epub_path in enumerate(untracked_epubs, 1):
                book_basename = os.path.basename(epub_path)
                if book_basename.lower().endswith(".epub"):
                    book_basename = book_basename[:-5]

                _status(f"[epub {idx}/{epub_total}] {book_basename}", added=added, updated=updated)

                stat_result = await conn.run(f'stat -c %Y "{epub_path}" 2>/dev/null', check=False)
                try:
                    mtime = int(stat_result.stdout.strip())
                except (ValueError, TypeError):
                    continue

                existing = storage.get_book_by_path(db_path, device_id, epub_path)
                # Phase 1 may have stored the path without .epub extension; check that too
                if existing is None and epub_path.lower().endswith(".epub"):
                    existing = storage.get_book_by_path(db_path, device_id, epub_path[:-5])
                if existing and existing.file_mtime == mtime and existing.cover_file:
                    continue
                # Don't create a duplicate record — reuse the path already in the DB if found
                effective_path = existing.file_path if existing else epub_path

                try:
                    existing_cover = existing.cover_file if existing else None
                    meta, cover_file = await _fetch_epub_meta_and_cover(
                        conn, epub_path, covers_dir, device_id, existing_cover
                    )
                    op = storage.upsert_book(
                        db_path, device_id, effective_path, mtime,
                        title=meta["title"] or book_basename,
                        authors=meta["authors"],
                        series=meta["series"],
                        series_index=meta["series_index"],
                        language=meta["language"],
                        pages=meta["pages"],
                        description=meta["description"],
                        cover_file=cover_file,
                        progress_pct=0.0,
                        md5=None,
                        status="tbr",
                    )
                    if op == "added":
                        added += 1
                    else:
                        updated += 1
                except Exception as e:
                    logger.warning("KoLibrary: failed to process epub %s: %s", epub_path, e)

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
