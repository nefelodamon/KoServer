import json
import os
from functools import lru_cache
from pathlib import Path


class Settings:
    def __init__(self):
        options = self._load_options()
        self.api_key: str = options.get("api_key", os.getenv("API_KEY", ""))
        self.ha_url: str = options.get("ha_url", os.getenv("HA_URL", "http://homeassistant:8123")).rstrip("/")
        self.data_dir: Path = Path(os.getenv("DATA_DIR", "/data"))
        self.portraits_dir: Path = self.data_dir / "portraits"
        self.db_path: Path = self.data_dir / "db" / "koserver.db"

    @staticmethod
    def _load_options() -> dict:
        options_path = Path("/data/options.json")
        if options_path.exists():
            try:
                return json.loads(options_path.read_text())
            except Exception:
                pass
        return {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
