import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.kolibrary import storage, sync

logger = logging.getLogger(__name__)

_INTERVALS = {
    "5m":     timedelta(minutes=5),
    "10m":    timedelta(minutes=10),
    "30m":    timedelta(minutes=30),
    "hourly": timedelta(hours=1),
    "6h":     timedelta(hours=6),
    "daily":  timedelta(hours=24),
    "weekly": timedelta(weeks=1),
}

_DEFER_INTERVALS = {
    "30m":   timedelta(minutes=30),
    "1h":    timedelta(hours=1),
    "2h":    timedelta(hours=2),
    "6h":    timedelta(hours=6),
    "12h":   timedelta(hours=12),
    "daily": timedelta(hours=24),
}


async def run_scheduler(db_path: Path, covers_dir: Path, key_path: Path) -> None:
    """Background task: wake every 30 s, sync devices that are due."""
    logger.info("KoLibrary scheduler started")
    while True:
        await asyncio.sleep(30)
        try:
            devices = storage.list_devices(db_path)
            now = datetime.now(tz=timezone.utc)
            for device in devices:
                interval = _INTERVALS.get(device.sync_interval)
                if interval is None:
                    continue  # manual
                if device.last_sync:
                    try:
                        last = datetime.fromisoformat(device.last_sync).replace(tzinfo=timezone.utc)
                        elapsed = now - last
                        if elapsed < interval:
                            continue
                        # If a defer is configured, last_sync is only set on success,
                        # so we can directly use it as the "last successful sync" time.
                        defer = _DEFER_INTERVALS.get(device.sync_defer)
                        if defer and elapsed < defer:
                            logger.debug(
                                "KoLibrary: device %d in defer window (%.0f min remaining), skipping",
                                device.id, (defer - elapsed).total_seconds() / 60,
                            )
                            continue
                    except ValueError:
                        pass
                logger.info("KoLibrary: scheduled sync for device %d (%s)", device.id, device.display_name)
                asyncio.create_task(sync.sync_device(device.id, db_path, covers_dir, key_path))
        except Exception as e:
            logger.error("KoLibrary scheduler error: %s", e)
