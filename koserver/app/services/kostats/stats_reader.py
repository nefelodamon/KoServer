import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass
class StatsSummary:
    total_hours: float
    books_read: int
    page_reads: int
    avg_speed: float
    current_book: str
    current_book_pct: int


@dataclass
class DayStat:
    date: str
    minutes: float


@dataclass
class MonthStat:
    month: str
    hours: float


@dataclass
class BookStat:
    title: str
    authors: str
    hours: float
    pages_per_hour: float
    started: str
    last_read: str
    status: str
    days_read: int = 0


@dataclass
class HourStat:
    hour: int
    minutes: float


@dataclass
class BookDetailStats:
    book_stat: "BookStat"
    daily: list["DayStat"]
    max_daily_minutes: float
    monthly: list["MonthStat"]
    max_monthly_hours: float
    by_hour: list["HourStat"]
    max_hour_minutes: float


@dataclass
class UserStats:
    summary: StatsSummary
    daily: list[DayStat]
    max_daily_minutes: float
    monthly: list[MonthStat]
    max_monthly_hours: float
    top_books: list[BookStat]
    all_books: list[BookStat]
    by_hour: list[HourStat]
    max_hour_minutes: float
    status_source: str = "pct:95"  # "kosync" or "pct:<n>"


def compute_stats(
    db_path: Path,
    kosync_db_path: Path | None = None,
    read_pct_threshold: int = 95,
) -> UserStats:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Load kosync in-progress MD5 set: presence = Reading, absence = Finished
    _kosync_reading: set[str] | None = None
    _kosync_path = kosync_db_path
    if _kosync_path and _kosync_path.is_file():
        try:
            kc = sqlite3.connect(str(_kosync_path))
            _kosync_reading = {
                r[0] for r in kc.execute("SELECT document FROM kosync_progress").fetchall()
            }
            kc.close()
        except Exception:
            _kosync_reading = None

    status_source = "kosync" if _kosync_reading is not None else f"pct:{read_pct_threshold}"

    # Totals
    r = conn.execute("""
        SELECT SUM(duration) as secs, COUNT(DISTINCT id_book) as books, COUNT(*) as pages
        FROM page_stat_data
    """).fetchone()
    total_secs = r["secs"] or 0
    books_read = r["books"] or 0
    page_reads = r["pages"] or 0
    total_hours = total_secs / 3600
    avg_speed = round(page_reads / total_hours, 1) if total_hours > 0 else 0

    # Current book (last opened with meaningful page count)
    cr = conn.execute("""
        SELECT b.title, MAX(p.page) as cur_page, p.total_pages
        FROM book b JOIN page_stat_data p ON p.id_book = b.id
        WHERE b.last_open = (SELECT MAX(last_open) FROM book WHERE pages > 50)
        GROUP BY b.id
    """).fetchone()
    current_book = cr["title"] if cr else ""
    current_pct = (
        min(round(cr["cur_page"] / cr["total_pages"] * 100), 100)
        if cr and cr["total_pages"] else 0
    )

    # Last 30 days
    rows = conn.execute("""
        SELECT date(start_time, 'unixepoch') as day, SUM(duration) / 60.0 as mins
        FROM page_stat_data
        WHERE start_time > strftime('%s', 'now', '-30 days')
        GROUP BY day
    """).fetchall()
    day_map = {r["day"]: r["mins"] for r in rows}
    today = date.today()
    daily = [
        DayStat(
            date=(today - timedelta(days=i)).isoformat(),
            minutes=day_map.get((today - timedelta(days=i)).isoformat(), 0),
        )
        for i in range(29, -1, -1)
    ]
    max_daily = max((d.minutes for d in daily), default=1) or 1

    # Monthly
    rows = conn.execute("""
        SELECT strftime('%Y-%m', start_time, 'unixepoch') as month,
               SUM(duration) / 3600.0 as hrs
        FROM page_stat_data GROUP BY month ORDER BY month
    """).fetchall()
    monthly = [MonthStat(month=r["month"], hours=round(r["hrs"], 1)) for r in rows]
    max_monthly = max((m.hours for m in monthly), default=1) or 1

    _BOOK_QUERY = """
        SELECT b.title, COALESCE(b.authors, '') as authors,
               SUM(p.duration) / 3600.0 as hrs,
               COUNT(*) * 1.0 / NULLIF(SUM(p.duration) / 3600.0, 0) as speed,
               date(MIN(p.start_time), 'unixepoch') as started,
               date(MAX(p.start_time), 'unixepoch') as last_read,
               MAX(p.page) as max_page,
               MAX(p.total_pages) as total_pages,
               p.id_book as book_id,
               b.md5 as md5
        FROM page_stat_data p JOIN book b ON b.id = p.id_book
        GROUP BY p.id_book
    """

    # Compute distinct reading days per (title, authors) key in Python.
    # Group by the same key used for deduplication so duplicate book entries
    # automatically union their day-sets rather than double-counting shared days.
    _book_keys: dict[int, tuple] = {}
    for _row in conn.execute(
        "SELECT id, title, COALESCE(authors, '') as authors FROM book"
    ).fetchall():
        _book_keys[_row["id"]] = (
            _row["title"].lower().strip(),
            _row["authors"].lower().strip(),
        )

    _raw_times = conn.execute(
        "SELECT id_book, start_time FROM page_stat_data WHERE start_time IS NOT NULL"
    ).fetchall()
    _day_sets_by_key: dict[tuple, set] = {}
    for _row in _raw_times:
        try:
            ts = int(_row["start_time"])
            if ts > 86400:  # exclude 0 / epoch-zero sentinel values
                key = _book_keys.get(_row["id_book"])
                if key:
                    if key not in _day_sets_by_key:
                        _day_sets_by_key[key] = set()
                    _day_sets_by_key[key].add(ts // 86400)
        except (TypeError, ValueError):
            pass
    _days_by_key: dict[tuple, int] = {k: len(v) for k, v in _day_sets_by_key.items()}

    def _make_book_stat(r) -> BookStat:
        key = (r["title"].lower().strip(), (r["authors"] or "").lower().strip())
        if _kosync_reading is not None:
            # book md5 present in kosync → still being read; absent → finished
            md5 = r["md5"] if "md5" in r.keys() else None
            status = "Reading" if (md5 and md5 in _kosync_reading) else "Finished"
        else:
            pct = (r["max_page"] / r["total_pages"] * 100) if r["total_pages"] else 0
            status = "Finished" if pct >= read_pct_threshold else "Reading"
        return BookStat(
            title=r["title"],
            authors=r["authors"] or "",
            hours=round(r["hrs"] or 0, 1),
            pages_per_hour=round(r["speed"] or 0, 1),
            started=r["started"] or "",
            last_read=r["last_read"] or "",
            status=status,
            days_read=_days_by_key.get(key, 0),
        )

    def _merge_duplicates(books: list[BookStat]) -> list[BookStat]:
        """Merge entries with the same title+authors, summing time and recalculating speed."""
        merged: dict[tuple, BookStat] = {}
        for b in books:
            key = (b.title.lower().strip(), b.authors.lower().strip())
            if key not in merged:
                merged[key] = BookStat(
                    title=b.title, authors=b.authors,
                    hours=b.hours, pages_per_hour=b.pages_per_hour,
                    started=b.started, last_read=b.last_read, status=b.status,
                    days_read=b.days_read,
                )
            else:
                m = merged[key]
                total_pages = m.pages_per_hour * m.hours + b.pages_per_hour * b.hours
                m.hours = round(m.hours + b.hours, 1)
                m.pages_per_hour = round(total_pages / m.hours, 1) if m.hours else 0
                m.started = min(m.started, b.started) if m.started and b.started else (m.started or b.started)
                m.last_read = max(m.last_read, b.last_read) if m.last_read and b.last_read else (m.last_read or b.last_read)
                if b.status == "Finished":
                    m.status = "Finished"
                # days_read already reflects the unioned set for this key; keep first value
        return list(merged.values())

    # Top books by time spent
    rows = conn.execute(
        _BOOK_QUERY + " HAVING hrs > 0.25 AND b.pages > 50 ORDER BY hrs DESC LIMIT 30"
    ).fetchall()
    top_books = _merge_duplicates([_make_book_stat(r) for r in rows])
    top_books.sort(key=lambda b: b.hours, reverse=True)
    top_books = top_books[:10]

    # All books
    rows = conn.execute(
        _BOOK_QUERY + " ORDER BY last_read DESC"
    ).fetchall()
    all_books = _merge_duplicates([_make_book_stat(r) for r in rows])
    all_books.sort(key=lambda b: b.last_read, reverse=True)
    books_read = len(all_books)

    # By hour of day (UTC — server runs UTC)
    rows = conn.execute("""
        SELECT CAST(strftime('%H', start_time, 'unixepoch') AS INTEGER) as hr,
               SUM(duration) / 60.0 as mins
        FROM page_stat_data GROUP BY hr ORDER BY hr
    """).fetchall()
    hour_map = {r["hr"]: r["mins"] for r in rows}
    by_hour = [HourStat(hour=h, minutes=hour_map.get(h, 0)) for h in range(24)]
    max_hour = max((h.minutes for h in by_hour), default=1) or 1

    conn.close()

    return UserStats(
        summary=StatsSummary(
            total_hours=round(total_hours, 1),
            books_read=books_read,
            page_reads=page_reads,
            avg_speed=avg_speed,
            current_book=current_book,
            current_book_pct=current_pct,
        ),
        daily=daily,
        max_daily_minutes=max_daily,
        monthly=monthly,
        max_monthly_hours=max_monthly,
        top_books=top_books,
        all_books=all_books,
        by_hour=by_hour,
        max_hour_minutes=max_hour,
        status_source=status_source,
    )


def get_book_detail_stats(
    db_path: Path,
    title: str,
    kosync_db_path: Path | None = None,
    read_pct_threshold: int = 95,
) -> BookDetailStats | None:
    """Return per-book stats for the book detail page, or None if no data found."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    _kosync_reading: set[str] | None = None
    if kosync_db_path and kosync_db_path.is_file():
        try:
            kc = sqlite3.connect(str(kosync_db_path))
            _kosync_reading = {r[0] for r in kc.execute("SELECT document FROM kosync_progress").fetchall()}
            kc.close()
        except Exception:
            pass

    book_rows = conn.execute(
        "SELECT id, md5, title, COALESCE(authors, '') as authors FROM book WHERE title = ? COLLATE NOCASE",
        (title,),
    ).fetchall()
    if not book_rows:
        conn.close()
        return None

    book_ids = [r["id"] for r in book_rows]
    ph = ",".join("?" * len(book_ids))

    r = conn.execute(f"""
        SELECT SUM(duration) / 3600.0 as hrs,
               COUNT(*) * 1.0 / NULLIF(SUM(duration) / 3600.0, 0) as speed,
               date(MIN(start_time), 'unixepoch') as started,
               date(MAX(start_time), 'unixepoch') as last_read,
               MAX(page) as max_page,
               MAX(total_pages) as total_pages
        FROM page_stat_data WHERE id_book IN ({ph})
    """, book_ids).fetchone()

    if not r or not r["hrs"]:
        conn.close()
        return None

    md5s = [row["md5"] for row in book_rows if row["md5"]]
    if _kosync_reading is not None:
        status = "Reading" if any(md5 in _kosync_reading for md5 in md5s) else "Finished"
    else:
        pct = (r["max_page"] / r["total_pages"] * 100) if r["total_pages"] else 0
        status = "Finished" if pct >= read_pct_threshold else "Reading"

    day_set: set = set()
    for ts_row in conn.execute(
        f"SELECT start_time FROM page_stat_data WHERE id_book IN ({ph}) AND start_time IS NOT NULL",
        book_ids,
    ).fetchall():
        try:
            ts = int(ts_row["start_time"])
            if ts > 86400:
                day_set.add(ts // 86400)
        except (TypeError, ValueError):
            pass

    first_book = book_rows[0]
    book_stat = BookStat(
        title=first_book["title"],
        authors=first_book["authors"] or "",
        hours=round(r["hrs"] or 0, 1),
        pages_per_hour=round(r["speed"] or 0, 1),
        started=r["started"] or "",
        last_read=r["last_read"] or "",
        status=status,
        days_read=len(day_set),
    )

    # Daily — last 30 days
    day_rows = conn.execute(f"""
        SELECT date(start_time, 'unixepoch') as day, SUM(duration) / 60.0 as mins
        FROM page_stat_data
        WHERE id_book IN ({ph}) AND start_time > strftime('%s', 'now', '-30 days')
        GROUP BY day
    """, book_ids).fetchall()
    day_map = {row["day"]: row["mins"] for row in day_rows}
    today = date.today()
    daily = [
        DayStat(
            date=(today - timedelta(days=i)).isoformat(),
            minutes=day_map.get((today - timedelta(days=i)).isoformat(), 0),
        )
        for i in range(29, -1, -1)
    ]
    max_daily = max((d.minutes for d in daily), default=1) or 1

    # Monthly
    month_rows = conn.execute(f"""
        SELECT strftime('%Y-%m', start_time, 'unixepoch') as month, SUM(duration) / 3600.0 as hrs
        FROM page_stat_data WHERE id_book IN ({ph}) GROUP BY month ORDER BY month
    """, book_ids).fetchall()
    monthly = [MonthStat(month=row["month"], hours=round(row["hrs"], 1)) for row in month_rows]
    max_monthly = max((m.hours for m in monthly), default=1) or 1

    # By hour of day
    hour_rows = conn.execute(f"""
        SELECT CAST(strftime('%H', start_time, 'unixepoch') AS INTEGER) as hr,
               SUM(duration) / 60.0 as mins
        FROM page_stat_data WHERE id_book IN ({ph}) GROUP BY hr ORDER BY hr
    """, book_ids).fetchall()
    hour_map = {row["hr"]: row["mins"] for row in hour_rows}
    by_hour = [HourStat(hour=h, minutes=hour_map.get(h, 0)) for h in range(24)]
    max_hour = max((h.minutes for h in by_hour), default=1) or 1

    conn.close()

    return BookDetailStats(
        book_stat=book_stat,
        daily=daily,
        max_daily_minutes=max_daily,
        monthly=monthly,
        max_monthly_hours=max_monthly,
        by_hour=by_hour,
        max_hour_minutes=max_hour,
    )
