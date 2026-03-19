import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.kolibrary import storage, sync

logger = logging.getLogger(__name__)

_INTERVALS = {
    "hourly": timedelta(hours=1),
    "6h":     timedelta(hours=6),
    "daily":  timedelta(hours=24),
    "weekly": timedelta(weeks=1),
}


async def run_scheduler(db_path: Path, covers_dir: Path, key_path: Path) -> None:
    """Background task: wake every minute, sync devices that are due."""
    logger.info("KoLibrary scheduler started")
    while True:
        await asyncio.sleep(60)
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
                        if now - last < interval:
                            continue
                    except ValueError:
                        pass
                logger.info("KoLibrary: scheduled sync for device %d (%s)", device.id, device.display_name)
                asyncio.create_task(sync.sync_device(device.id, db_path, covers_dir, key_path))
        except Exception as e:
            logger.error("KoLibrary scheduler error: %s", e)
