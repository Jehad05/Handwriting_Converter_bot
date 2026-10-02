"""Atomically download the four optional Google Fonts used by the converter."""
from __future__ import annotations

import os
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

FONTS_DIR = Path(__file__).resolve().parent
MAX_FONT_BYTES = 20 * 1024 * 1024
ALLOWED_HOST = "github.com"
FONTS = {
    "Kalam-Regular.ttf": "https://github.com/google/fonts/raw/main/ofl/kalam/Kalam-Regular.ttf",
    "Kalam-Bold.ttf": "https://github.com/google/fonts/raw/main/ofl/kalam/Kalam-Bold.ttf",
    "Amiri-Regular.ttf": "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf",
    "Amiri-Bold.ttf": "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Bold.ttf",
}


def _valid_font_header(data: bytes) -> bool:
    return data[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1")


def download(name: str, url: str) -> bool:
    if name not in FONTS or url != FONTS[name]:
        raise ValueError("font name/URL is not in the allowlisted manifest")
    if urlparse(url).hostname != ALLOWED_HOST:
        raise ValueError("font host is not allowlisted")
    dest = FONTS_DIR / name
    if dest.is_file() and dest.stat().st_size > 0:
        print(f"Already present: {name}")
        return True

    temp_path = None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "telegram-pdf-bot-font-setup/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            with tempfile.NamedTemporaryFile(
                prefix=f".{name}.", suffix=".part", dir=FONTS_DIR, delete=False
            ) as temp:
                temp_path = Path(temp.name)
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_FONT_BYTES:
                        raise ValueError("font exceeds the download size limit")
                    temp.write(chunk)
                temp.flush()
                os.fsync(temp.fileno())
        data = temp_path.read_bytes()
        if not _valid_font_header(data):
            raise ValueError("downloaded file is not a recognized TrueType/OpenType font")
        os.replace(temp_path, dest)
        temp_path = None
        print(f"Downloaded: {name} ({len(data)} bytes)")
        return True
    except Exception as exc:
        print(f"Download failed for {name}: {type(exc).__name__}")
        return False
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main() -> int:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    failures = [name for name, url in FONTS.items() if not download(name, url)]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
