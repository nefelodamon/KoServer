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


def compute_stats(db_path: Path) -> UserStats:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

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
               p.id_book as book_id
        FROM page_stat_data p JOIN book b ON b.id = p.id_book
        GROUP BY p.id_book
    """

    # Dedicated days-read query: same epoch-division approach used by the calendar endpoint,
    # run as a standalone query so it isn't affected by the complex GROUP BY context.
    _days_rows = conn.execute("""
        SELECT id_book, COUNT(DISTINCT CAST(start_time / 86400 AS INTEGER)) as dr
        FROM page_stat_data
        WHERE start_time > 0
        GROUP BY id_book
    """).fetchall()
    _days_by_id: dict[int, int] = {r["id_book"]: (r["dr"] or 0) for r in _days_rows}

    def _make_book_stat(r) -> BookStat:
        pct = (r["max_page"] / r["total_pages"] * 100) if r["total_pages"] else 0
        return BookStat(
            title=r["title"],
            authors=r["authors"] or "",
            hours=round(r["hrs"] or 0, 1),
            pages_per_hour=round(r["speed"] or 0, 1),
            started=r["started"] or "",
            last_read=r["last_read"] or "",
            status="Finished" if pct >= 90 else "Reading",
            days_read=_days_by_id.get(r["book_id"], 0),
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
                m.days_read += b.days_read
        return list(merged.values())

    # Top books by time spent
    rows = conn.execute(
        _BOOK_QUERY + " HAVING hrs > 0.25 AND b.pages > 50 ORDER BY hrs DESC LIMIT 30"
    ).fetchall()
    top_books = _merge_duplicates([_make_book_stat(r) for r in rows])
    top_books.sort(key=lambda b: b.hours, reverse=True)
    top_books = top_books[:10]

    # All books (computed before syncing days_read back to top_books)
    rows = conn.execute(
        _BOOK_QUERY + " ORDER BY last_read DESC"
    ).fetchall()
    all_books = _merge_duplicates([_make_book_stat(r) for r in rows])
    all_books.sort(key=lambda b: b.last_read, reverse=True)
    books_read = len(all_books)

    # Sync days_read from all_books (full merge) into top_books
    _all_days = {(b.title.lower().strip(), b.authors.lower().strip()): b.days_read for b in all_books}
    for b in top_books:
        key = (b.title.lower().strip(), b.authors.lower().strip())
        b.days_read = _all_days.get(key, b.days_read)

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
    )
