import json
import os
from functools import lru_cache
from pathlib import Path


class Settings:
    def __init__(self):
        options = self._load_options()
        self.api_key: str = options.get("api_key", os.getenv("API_KEY", ""))
        self.ha_url: str = options.get("ha_url", os.getenv("HA_URL", "")).rstrip("/")
        self.ssl_certificate: str = options.get("ssl_certificate", os.getenv("SSL_CERTIFICATE", "/ssl/fullchain.pem"))
        self.ssl_key: str = options.get("ssl_key", os.getenv("SSL_KEY", "/ssl/privkey.pem"))
        self.data_dir: Path = Path(os.getenv("DATA_DIR", "/data"))
        self.share_dir: Path = Path("/share/koserver")
        self.kocharacters_dir: Path = self.share_dir / "kocharacters"
        self.portraits_dir: Path = self.kocharacters_dir / "portraits"
        self.kocharacters_db_path: Path = self.kocharacters_dir / "kocharacters.db"
        self.kostats_dir: Path = self.share_dir / "kostats"
        self.kostats_db_path: Path = self.kostats_dir / "kostats.db"
        self.kosync_dir: Path = self.share_dir / "kosync"
        self.kosync_db_path: Path = self.kosync_dir / "kosync.db"
        self.kolibrary_dir: Path = self.share_dir / "kolibrary"
        self.kolibrary_db_path: Path = self.kolibrary_dir / "kolibrary.db"
        self.kolibrary_covers_dir: Path = self.kolibrary_dir / "covers"
        self.kolibrary_key_path: Path = self.data_dir / "kolibrary.key"

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
