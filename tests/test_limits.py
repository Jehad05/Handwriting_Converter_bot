from __future__ import annotations

import sqlite3
import os
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import limits


class LimitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = limits.DB_PATH
        self.old_data = limits.DATA_DIR
        self.old_limit = limits.DAILY_LIMIT
        limits.DB_PATH = Path(self.tmp.name) / "data" / "bot.sqlite3"
        limits.DATA_DIR = Path(self.tmp.name) / "data"
        limits.DAILY_LIMIT = 3
        limits.init_db()

    def tearDown(self):
        limits.DB_PATH = self.old_db
        limits.DATA_DIR = self.old_data
        limits.DAILY_LIMIT = self.old_limit
        self.tmp.cleanup()

    def test_database_directory_and_sqlite_files_are_private(self):
        self.assertEqual(stat.S_IMODE(limits.DATA_DIR.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(limits.DB_PATH.stat().st_mode), 0o600)

        with limits._conn() as writer:
            writer.execute("CREATE TABLE IF NOT EXISTS permission_probe (value INTEGER)")
            writer.execute("INSERT INTO permission_probe VALUES (1)")
            with limits._conn():
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{limits.DB_PATH}{suffix}")
                    self.assertTrue(sidecar.is_file(), sidecar.name)
                    self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)

    def test_shared_tmp_db_path_is_rejected_without_changing_tmp_permissions(self):
        shared_dir = Path("/tmp")
        before = shared_dir.stat()
        self.assertTrue(before.st_mode & (stat.S_ISVTX | 0o022))
        old_db = limits.DB_PATH
        db_path = shared_dir / f"bot-permission-test-{os.getpid()}-{id(self)}.sqlite3"
        try:
            limits.DB_PATH = db_path
            with self.assertRaises(PermissionError):
                limits.init_db()
            after = shared_dir.stat()
            self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
            self.assertEqual(after.st_mode & stat.S_ISVTX, before.st_mode & stat.S_ISVTX)
            self.assertEqual(after.st_uid, before.st_uid)
            for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
                self.assertFalse(path.exists(), path.name)
        finally:
            limits.DB_PATH = old_db

    def test_quota_window_resets_at_0200_utc(self):
        before = datetime(2026, 10, 1, 1, 59, tzinfo=timezone.utc)
        at_reset = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(limits.current_window_start(before),
                         datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(limits.current_window_start(at_reset), at_reset)
        self.assertEqual(limits.next_reset(before), at_reset)

    def test_delivered_conversions_from_before_0200_do_not_consume_new_window(self):
        before = datetime(2026, 10, 1, 1, 59, tzinfo=timezone.utc)
        at_reset = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)
        for _ in range(limits.DAILY_LIMIT):
            reservation = limits.reserve(100, before)
            self.assertIsNotNone(reservation)
            self.assertTrue(limits.commit_reservation(reservation, before))
        self.assertIsNone(limits.reserve(100, before))
        self.assertEqual(limits.get_usage(100)[0], 0)
        self.assertIsNotNone(limits.reserve(100, at_reset))

    def test_concurrent_reservations_never_exceed_limit(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: limits.reserve(101, now), range(30)))
        accepted = [value for value in results if value is not None]
        self.assertEqual(len(accepted), limits.DAILY_LIMIT)
        self.assertEqual(limits.remaining(101), 0)

    def test_pre_delivery_refund_frees_reservation(self):
        reservation = limits.reserve(102)
        self.assertIsNotNone(reservation)
        self.assertTrue(limits.refund_reservation(reservation))
        self.assertEqual(limits.remaining(102), limits.DAILY_LIMIT)

    def test_committed_reservation_cannot_be_refunded(self):
        reservation = limits.reserve(103)
        self.assertTrue(limits.commit_reservation(reservation))
        self.assertFalse(limits.refund_reservation(reservation))
        self.assertEqual(limits.remaining(103), limits.DAILY_LIMIT - 1)

    def test_stale_reservations_are_recovered(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        stale = now - limits.RESERVATION_TTL - timedelta(seconds=1)
        reservation = limits.reserve(104, stale)
        self.assertIsNotNone(reservation)
        self.assertEqual(limits.recover_stale_reservations(now), 1)
        self.assertEqual(limits.remaining(104), limits.DAILY_LIMIT)

    def test_heartbeat_keeps_active_reservation_charged_past_original_ttl(self):
        started = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        reservation = limits.reserve(106, started)
        self.assertIsNotNone(reservation)
        heartbeat = started + limits.RESERVATION_TTL - timedelta(minutes=1)
        self.assertTrue(limits.renew_reservation(reservation, heartbeat))

        beyond_original_ttl = started + limits.RESERVATION_TTL + timedelta(seconds=1)
        self.assertEqual(limits.recover_stale_reservations(beyond_original_ttl), 0)
        self.assertEqual(limits.remaining(106), limits.DAILY_LIMIT - 1)

    def test_concurrent_cleaners_recover_stale_reservation_only_once(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        stale = now - limits.RESERVATION_TTL - timedelta(seconds=1)
        reservation = limits.reserve(107, stale)
        self.assertIsNotNone(reservation)
        with ThreadPoolExecutor(max_workers=12) as pool:
            recovered = list(pool.map(
                lambda _: limits.recover_stale_reservations(now), range(24)
            ))
        self.assertEqual(sum(recovered), 1)
        self.assertEqual(limits.remaining(107), limits.DAILY_LIMIT)

    def test_previous_schema_pending_reservation_migrates_for_restart_recovery(self):
        db_path = Path(self.tmp.name) / "previous.sqlite3"
        reserved_at = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE conversions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "user_id INTEGER NOT NULL, status TEXT NOT NULL, reserved_at TEXT NOT NULL, "
                "delivered_at TEXT)"
            )
            conn.execute(
                "INSERT INTO conversions(user_id, status, reserved_at) VALUES(?, 'reserved', ?)",
                (108, reserved_at.isoformat()),
            )

        limits.DB_PATH = db_path
        limits.init_db()
        with sqlite3.connect(db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(conversions)")}
        self.assertTrue({"heartbeat_at", "lease_owner"}.issubset(columns))
        self.assertFalse(limits.renew_reservation(1, reserved_at + timedelta(minutes=1)))
        self.assertEqual(
            limits.recover_stale_reservations(
                reserved_at + limits.RESERVATION_TTL + timedelta(seconds=1)
            ),
            1,
        )

    def test_migrates_original_delivered_conversions(self):
        legacy_path = Path(self.tmp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as conn:
            conn.execute(
                "CREATE TABLE conversions(id INTEGER PRIMARY KEY, user_id INTEGER, used_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO conversions(user_id, used_at) VALUES(?, ?)",
                (105, datetime.now(timezone.utc).isoformat()),
            )
        limits.DB_PATH = legacy_path
        limits.init_db()
        self.assertEqual(limits.get_usage(105)[0], 1)
        self.assertEqual(limits.remaining(105), limits.DAILY_LIMIT - 1)
        with sqlite3.connect(legacy_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(conversions)")}
        self.assertTrue({"heartbeat_at", "lease_owner"}.issubset(columns))


if __name__ == "__main__":
    unittest.main()
