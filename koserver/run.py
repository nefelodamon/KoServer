import os
import sys

from app.config import get_settings


def main():
    settings = get_settings()
    args = [
        sys.executable, "-m", "uvicorn",
        "app.main:app",
        "--host", "0.0.0.0",
        "--port", "8099",
        "--ssl-certfile", settings.ssl_certificate,
        "--ssl-keyfile", settings.ssl_key,
        "--log-level", "info",
    ]
    os.execv(sys.executable, args)


if __name__ == "__main__":
    main()
