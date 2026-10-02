"""Daily conversion quota with atomic reservations and recoverable pending work."""
from __future__ import annotations

import logging
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from config import DATA_DIR, DB_PATH, PROJECT_DIR, positive_int_env

logger = logging.getLogger(__name__)
DAILY_LIMIT = positive_int_env("DAILY_LIMIT", 3)
PREMIUM_DAILY_LIMIT: int | None = None  # None means unlimited premium conversions.
RESET_HOUR_UTC = 2
RESERVATION_TTL = timedelta(hours=2)
RESERVATION_HEARTBEAT_INTERVAL = timedelta(minutes=5)
_PROCESS_LEASE_OWNER = uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_private_data_directory(directory: Path) -> Path:
    """Create/tighten only the app-owned data directory, never shared ancestors."""
    directory = Path(directory)
    if directory.is_symlink():
        raise PermissionError("DATA_DIR must not be a symbolic link")
    directory = directory.resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), PROJECT_DIR.resolve()}
    if directory in forbidden:
        raise PermissionError("refusing to use a non-dedicated DATA_DIR")

    missing: list[Path] = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            raise PermissionError("DATA_DIR has no existing parent")
        cursor = parent
    if not cursor.is_dir():
        raise PermissionError("DATA_DIR parent is not a directory")

    # Create only missing app-specific path components with private permissions.
    # Existing shared ancestors (for example /tmp) are never chmodded.
    for new_directory in reversed(missing):
        new_directory.mkdir(mode=0o700)
        info = new_directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError("new DATA_DIR component is not owned by this process")
        new_directory.chmod(0o700)

    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise PermissionError("DATA_DIR must be a directory owned by this process")
    if info.st_mode & (stat.S_ISVTX | 0o022):
        raise PermissionError("refusing a shared or sticky DATA_DIR")
    directory.chmod(0o700)
    return directory


def _require_private_custom_db_directory(directory: Path) -> None:
    """Accept custom DB_PATH parents only when already private and user-owned."""
    if not directory.is_dir():
        raise PermissionError("custom DB_PATH parent must already exist")
    resolved = directory.resolve()
    info = resolved.stat()
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & stat.S_ISVTX or mode & 0o077
            or (mode & 0o700) != 0o700):
        raise PermissionError("custom DB_PATH parent must be a private 0700 directory owned by this process")


def _ensure_private_db_directory(db_path: Path) -> None:
    """Secure DATA_DIR or reject a custom database path in a shared directory."""
    data_dir = Path(DATA_DIR).resolve()
    directory = Path(db_path).parent.resolve()
    if directory == data_dir:
        _ensure_private_data_directory(Path(DATA_DIR))
    else:
        _require_private_custom_db_directory(directory)


def _sqlite_file_paths(db_path: Path) -> tuple[Path, ...]:
    return (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"),
            Path(f"{db_path}-journal"))


def _assert_safe_sqlite_files(db_path: Path) -> None:
    """Reject symlinks, non-regular files, and hard-linked database artifacts."""
    for path in _sqlite_file_paths(db_path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError(f"unsafe SQLite file path: {path.name}")


def _restrict_db_file_permissions(db_path: Path) -> None:
    """Restrict the database and SQLite sidecar files to the current user."""
    _assert_safe_sqlite_files(db_path)
    for path in _sqlite_file_paths(db_path):
        try:
            path.chmod(0o600)
        except FileNotFoundError:
            pass


def _utc(value: datetime | None = None) -> datetime:
    value = value or _now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    db_path = Path(DB_PATH)
    _ensure_private_db_directory(db_path)
    _assert_safe_sqlite_files(db_path)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        _restrict_db_file_permissions(db_path)
        yield conn
        if conn.in_transaction:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        try:
            _restrict_db_file_permissions(db_path)
        finally:
            conn.close()


def _create_conversions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE conversions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('reserved', 'committed')),
            reserved_at TEXT NOT NULL,
            delivered_at TEXT,
            heartbeat_at TEXT,
            lease_owner TEXT
        )"""
    )


def init_db() -> None:
    """Create current schema and migrate the original delivered-only table."""
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "conversions" not in names:
            _create_conversions(conn)
        else:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(conversions)")}
            if not {"status", "reserved_at", "delivered_at"}.issubset(columns):
                conn.execute("ALTER TABLE conversions RENAME TO conversions_legacy")
                _create_conversions(conn)
                legacy_columns = {row[1] for row in conn.execute(
                    "PRAGMA table_info(conversions_legacy)"
                )}
                if {"id", "user_id", "used_at"}.issubset(legacy_columns):
                    conn.execute(
                        """INSERT INTO conversions(id, user_id, status, reserved_at, delivered_at)
                           SELECT id, user_id, 'committed', used_at, used_at
                           FROM conversions_legacy"""
                    )
                conn.execute("DROP TABLE conversions_legacy")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversions)")}
        if "heartbeat_at" not in columns:
            conn.execute("ALTER TABLE conversions ADD COLUMN heartbeat_at TEXT")
        if "lease_owner" not in columns:
            conn.execute("ALTER TABLE conversions ADD COLUMN lease_owner TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversions_user_status_time "
            "ON conversions(user_id, status, delivered_at, reserved_at)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS premium_users (
                user_id INTEGER PRIMARY KEY,
                expires_at TEXT NOT NULL,
                granted_by INTEGER,
                granted_at TEXT NOT NULL
            )"""
        )
        conn.commit()


def current_window_start(now: datetime | None = None) -> datetime:
    now = _utc(now)
    start = now.replace(hour=RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    if now < start:
        start -= timedelta(days=1)
    return start


def next_reset(now: datetime | None = None) -> datetime:
    return current_window_start(now) + timedelta(days=1)


def format_time_until_reset(now: datetime | None = None) -> str:
    delta = next_reset(now) - _utc(now)
    hours, rem = divmod(max(0, int(delta.total_seconds())), 3600)
    return f"{hours} ساعة و{rem // 60} دقيقة"


def _limit_in_transaction(conn: sqlite3.Connection, user_id: int,
                          now: datetime) -> int | None:
    row = conn.execute(
        "SELECT expires_at FROM premium_users WHERE user_id=?", (user_id,)
    ).fetchone()
    if row:
        try:
            expiry = _utc(datetime.fromisoformat(row[0]))
        except (TypeError, ValueError):
            expiry = now
        if expiry > now:
            return PREMIUM_DAILY_LIMIT
    return DAILY_LIMIT


def _counts_in_transaction(conn: sqlite3.Connection, user_id: int,
                           now: datetime) -> tuple[int, int]:
    start = current_window_start(now).isoformat()
    committed = conn.execute(
        "SELECT COUNT(*) FROM conversions WHERE user_id=? "
        "AND status='committed' AND delivered_at>=?", (user_id, start)
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM conversions WHERE user_id=? AND status='reserved'",
        (user_id,),
    ).fetchone()[0]
    return int(committed), int(pending)


def get_usage(user_id: int) -> tuple[int, int | None]:
    """Return successfully delivered conversions in this reset window and limit."""
    now = _now()
    with _conn() as conn:
        limit = _limit_in_transaction(conn, user_id, now)
        used, _pending = _counts_in_transaction(conn, user_id, now)
    return used, limit


def remaining(user_id: int) -> int | None:
    """Return slots left, including any active in-flight reservations."""
    now = _now()
    with _conn() as conn:
        limit = _limit_in_transaction(conn, user_id, now)
        used, pending = _counts_in_transaction(conn, user_id, now)
    if limit is None:
        return None
    return max(0, limit - used - pending)


def reserve(user_id: int, now: datetime | None = None) -> int | None:
    """Atomically reserve one slot; return its id or None when quota is full."""
    now = _utc(now)
    stale_before = (now - RESERVATION_TTL).isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM conversions WHERE status='reserved' "
            "AND COALESCE(heartbeat_at, reserved_at)<?",
            (stale_before,),
        )
        limit = _limit_in_transaction(conn, user_id, now)
        used, pending = _counts_in_transaction(conn, user_id, now)
        if limit is not None and used + pending >= limit:
            return None
        cursor = conn.execute(
            "INSERT INTO conversions(user_id, status, reserved_at, delivered_at, "
            "heartbeat_at, lease_owner) VALUES(?, 'reserved', ?, NULL, ?, ?)",
            (user_id, now.isoformat(), now.isoformat(), _PROCESS_LEASE_OWNER),
        )
        return int(cursor.lastrowid)


def renew_reservation(reservation_id: int, now: datetime | None = None) -> bool:
    """Extend this process's pending reservation lease; false means it is no longer owned."""
    heartbeat_at = _utc(now).isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE conversions SET heartbeat_at=? WHERE id=? AND status='reserved' "
            "AND lease_owner=?",
            (heartbeat_at, reservation_id, _PROCESS_LEASE_OWNER),
        )
        return cursor.rowcount == 1


def commit_reservation(reservation_id: int, now: datetime | None = None) -> bool:
    """Mark a reservation consumed only after Telegram confirms document delivery."""
    delivered_at = _utc(now).isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE conversions SET status='committed', delivered_at=? "
            "WHERE id=? AND status='reserved'", (delivered_at, reservation_id)
        )
        return cursor.rowcount == 1


def refund_reservation(reservation_id: int | None) -> bool:
    """Release a reservation after conversion or pre-delivery failure only."""
    if reservation_id is None:
        return False
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "DELETE FROM conversions WHERE id=? AND status='reserved'", (reservation_id,)
        )
        return cursor.rowcount == 1


def recover_stale_reservations(now: datetime | None = None) -> int:
    """Release leases with no heartbeat for two hours, atomically with concurrent cleaners."""
    cutoff = (_utc(now) - RESERVATION_TTL).isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "DELETE FROM conversions WHERE status='reserved' "
            "AND COALESCE(heartbeat_at, reserved_at)<?", (cutoff,)
        )
        recovered = cursor.rowcount
    if recovered:
        logger.info("recovered_stale_reservations count=%d", recovered)
    return recovered


def cleanup_old(days: int = 3, now: datetime | None = None) -> int:
    """Trim old committed history after its quota window has elapsed."""
    cutoff = (_utc(now) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "DELETE FROM conversions WHERE status='committed' AND delivered_at<?", (cutoff,)
        )
        return cursor.rowcount


# Small compatibility surface for integrations using the previous function names.
def try_consume(user_id: int) -> int | None:
    return reserve(user_id)


def refund(conversion_id: int | None) -> bool:
    return refund_reservation(conversion_id)


def is_premium(user_id: int) -> bool:
    return premium_expiry(user_id) is not None


def grant_premium(user_id: int, days: int, granted_by: int | None = None) -> datetime:
    if days <= 0:
        raise ValueError("days must be positive")
    now = _now()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT expires_at FROM premium_users WHERE user_id=?", (user_id,)
        ).fetchone()
        base = now
        if row:
            current = _utc(datetime.fromisoformat(row[0]))
            if current > now:
                base = current
        expires = base + timedelta(days=days)
        conn.execute(
            "INSERT INTO premium_users(user_id, expires_at, granted_by, granted_at) "
            "VALUES(?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "expires_at=excluded.expires_at, granted_by=excluded.granted_by, "
            "granted_at=excluded.granted_at",
            (user_id, expires.isoformat(), granted_by, now.isoformat()),
        )
    return expires


def revoke_premium(user_id: int) -> bool:
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))
        return cursor.rowcount > 0


def premium_expiry(user_id: int) -> datetime | None:
    now = _now()
    with _conn() as conn:
        row = conn.execute(
            "SELECT expires_at FROM premium_users WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row:
        return None
    expiry = _utc(datetime.fromisoformat(row[0]))
    return expiry if expiry > now else None
