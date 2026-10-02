"""Central project configuration with paths anchored to this file, not cwd."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # lets offline utility tests run before requirements are installed
    def load_dotenv(*_args, **_kwargs):
        return False

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env", override=False)


def project_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


DATA_DIR = project_path(os.environ.get("DATA_DIR", "data"))
TEMP_DIR = project_path(os.environ.get("TEMP_DIR", str(DATA_DIR / "tmp")))
DB_PATH = project_path(os.environ.get("DB_PATH", str(DATA_DIR / "bot.sqlite3")))
FONT_DIR = PROJECT_DIR / "fonts"


def positive_int_env(name: str, default: int) -> int:
    """Read a positive integer; invalid, zero, or negative values use default."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def bounded_float_env(name: str, default: float, minimum: float,
                      maximum: float) -> float:
    """Read a finite numeric environment setting within an allowed safety range."""
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not value == value or value in (float("inf"), float("-inf")):
        return default
    return value if minimum <= value <= maximum else default


# Defaults retain headroom below Telegram's approximate ~30/s broadcast limit.
# Per-chat minimums are enforced again in the limiter to prevent accidental bursts.
BOT_GLOBAL_SENDS_PER_SECOND = bounded_float_env(
    "BOT_GLOBAL_SENDS_PER_SECOND", 20.0, 1.0, 25.0
)
BOT_PRIVATE_CHAT_INTERVAL_SECONDS = bounded_float_env(
    "BOT_PRIVATE_CHAT_INTERVAL_SECONDS", 1.0, 1.0, 3600.0
)
BOT_GROUP_CHAT_INTERVAL_SECONDS = bounded_float_env(
    "BOT_GROUP_CHAT_INTERVAL_SECONDS", 3.05, 3.0, 3600.0
)
PDF_CONVERSION_CONCURRENCY = positive_int_env("PDF_CONVERSION_CONCURRENCY", 2)


try:
    ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0"))
except ValueError as exc:
    raise RuntimeError("ADMIN_USER_ID must be an integer") from exc
