Enterfrom __future__ import annotations

import asyncio
import threading
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import limits
import main
from outbound import DeliveryOutcomeUncertain


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.deleted = False
        self.edits = []

    async def delete(self):
        self.deleted = True
        raise RuntimeError("post-delivery notice cleanup failed")

    async def edit_text(self, text):
        self.edits.append(text)


class FakeBot:
    def __init__(self, fail_after_progress=False, fail_progress=False):
        self.messages = []
        self.documents = []
        self.fail_after_progress = fail_after_progress
        self.fail_progress = fail_progress
        self.fail_document = False

    async def send_message(self, chat_id, text):
        if self.fail_progress and not self.messages:
            self.fail_progress = False
            raise RuntimeError("simulated progress-message failure")
        if self.fail_after_progress and self.messages:
            raise RuntimeError("post-delivery notice failed")
        msg = FakeMessage(text)
        self.messages.append(msg)
        return msg

    async def send_document(self, **kwargs):
        if self.fail_document:
            raise RuntimeError("simulated delivery failure")
        self.documents.append(kwargs)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = limits.DB_PATH
        self.old_data = limits.DATA_DIR
        self.old_temp = main.TEMP_DIR
        self.old_limit = limits.DAILY_LIMIT
        limits.DB_PATH = Path(self.tmp.name) / "state" / "bot.db"
        limits.DATA_DIR = Path(self.tmp.name) / "state"
        limits.DAILY_LIMIT = 3
        main.TEMP_DIR = Path(self.tmp.name) / "tmp"
        limits.init_db()

    def tearDown(self):
        limits.DB_PATH = self.old_db
        limits.DATA_DIR = self.old_data
        main.TEMP_DIR = self.old_temp
        limits.DAILY_LIMIT = self.old_limit
        self.tmp.cleanup()

    async def test_conversion_failure_refunds_and_cleans_temp_pdf(self):
        bot = FakeBot()
        with patch.object(main, "create_pdf", side_effect=ValueError("SECRET_MESSAGE_SHOULD_NOT_LOG")), \
             self.assertLogs(main.logger, level="ERROR") as captured:
            result = await main._send_pdf(bot, 1, 2, ["private message"], "title")
        self.assertFalse(result)
        self.assertEqual(limits.remaining(2), 3)
        self.assertEqual(list(main.TEMP_DIR.glob("*.pdf")), [])
        self.assertNotIn("private message", "\n".join(captured.output))
        self.assertNotIn("SECRET_MESSAGE_SHOULD_NOT_LOG", "\n".join(captured.output))
        self.assertTrue(bot.messages[0].edits)

    async def test_delivery_failure_refunds_and_cleans_temp_pdf(self):
        bot = FakeBot()
        bot.fail_document = True
        with patch.object(main, "create_pdf", side_effect=lambda text, path: Path(path).write_bytes(b"pdf")):
            result = await main._send_pdf(bot, 1, 3, ["text"], None)
        self.assertFalse(result)
        self.assertEqual(limits.remaining(3), 3)
        self.assertEqual(list(main.TEMP_DIR.glob("*.pdf")), [])

    async def test_uncertain_document_delivery_keeps_quota_reservation_and_warns(self):
        bot = FakeBot()

        async def ambiguous_delivery(**_kwargs):
            raise DeliveryOutcomeUncertain("mocked timeout after upload")

        bot.send_document = ambiguous_delivery
        with patch.object(main, "create_pdf", side_effect=lambda text, path: Path(path).write_bytes(b"pdf")):
            result = await main._send_pdf(bot, 1, 31, ["text"], None)
        self.assertFalse(result)
        self.assertEqual(limits.get_usage(31)[0], 0)
        self.assertEqual(limits.remaining(31), 2)  # Reservation remains pending; no refund.
        self.assertIn("لم تتم إعادة المحاولة تلقائيًا", bot.messages[0].edits[-1])
        self.assertEqual(list(main.TEMP_DIR.glob("*.pdf")), [])

    async def test_progress_message_failure_refunds_and_cleans_temp_pdf(self):
        bot = FakeBot(fail_progress=True)
        with patch.object(main, "create_pdf") as create:
            result = await main._send_pdf(bot, 1, 30, ["text"], None)
        self.assertFalse(result)
        create.assert_not_called()
        self.assertEqual(limits.remaining(30), 3)
        self.assertEqual(list(main.TEMP_DIR.glob("*.pdf")), [])

    async def test_successful_delivery_is_not_refunded_for_post_delivery_failures(self):
        bot = FakeBot(fail_after_progress=True)
        with patch.object(main, "create_pdf", side_effect=lambda text, path: Path(path).write_bytes(b"pdf")):
            result = await main._send_pdf(bot, 1, 4, ["text"], None)
        self.assertTrue(result)
        self.assertEqual(len(bot.documents), 1)
        self.assertEqual(limits.get_usage(4)[0], 1)
        self.assertEqual(limits.remaining(4), 2)
        self.assertEqual(list(main.TEMP_DIR.glob("*.pdf")), [])

    async def test_successful_delivery_deletes_temp_pdf_even_when_cleanup_raises(self):
        bot = FakeBot()
        with patch.object(main, "create_pdf", side_effect=lambda text, path: Path(path).write_bytes(b"pdf")):
            result = await main._send_pdf(bot, 1, 5, ["text"], None)
        self.assertTrue(result)
        self.assertTrue(bot.messages[0].deleted)
        self.assertEqual(list(main.TEMP_DIR.glob("*.pdf")), [])
        self.assertEqual(limits.remaining(5), 2)

    async def test_bookkeeping_failure_after_delivery_does_not_refund_reservation(self):
        bot = FakeBot()
        with patch.object(main, "create_pdf", side_effect=lambda text, path: Path(path).write_bytes(b"pdf")), \
             patch.object(main.limits, "commit_reservation", side_effect=RuntimeError("db unavailable")):
            result = await main._send_pdf(bot, 1, 6, ["text"], None)
        self.assertTrue(result)
        self.assertEqual(len(bot.documents), 1)
        self.assertEqual(limits.get_usage(6)[0], 0)
        self.assertEqual(limits.remaining(6), 2)  # pending slot stays reserved; no post-delivery refund
        self.assertEqual(list(main.TEMP_DIR.glob("*.pdf")), [])

    async def test_active_delivery_heartbeat_survives_two_hours_of_concurrent_cleanup(self):
        started = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        clock_lock = threading.Lock()
        clock_value = [started]
        renewal_lock = threading.Lock()
        renewal_times = []
        renewal_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_now():
            with clock_lock:
                return clock_value[0]

        def advance_clock(delta):
            with clock_lock:
                clock_value[0] += delta
                return clock_value[0]

        original_renew = limits.renew_reservation

        def observed_renew(reservation_id, now=None):
            renewed = original_renew(reservation_id, now=now or fake_now())
            if renewed:
                with renewal_lock:
                    renewal_times.append(now or fake_now())
                loop.call_soon_threadsafe(renewal_event.set)
            return renewed

        class GatedBot(FakeBot):
            def __init__(self):
                super().__init__()
                self.document_started = asyncio.Event()
                self.allow_delivery = asyncio.Event()

            async def send_document(self, **kwargs):
                self.document_started.set()
                await self.allow_delivery.wait()
                self.documents.append(kwargs)

        bot = GatedBot()
        task = None
        with patch.object(limits, "_now", side_effect=fake_now), \
             patch.object(limits, "renew_reservation", side_effect=observed_renew), \
             patch.object(limits, "RESERVATION_HEARTBEAT_INTERVAL", timedelta(milliseconds=5)), \
             patch.object(main, "create_pdf", side_effect=lambda text, path: Path(path).write_bytes(b"pdf")):
            task = asyncio.create_task(main._send_pdf(bot, 1, 81, ["long-running"], None))
            try:
                await asyncio.wait_for(bot.document_started.wait(), timeout=2)
                deadline = advance_clock(limits.RESERVATION_TTL + timedelta(seconds=1))

                # Wait until the running task renews at the advanced fake-clock time.
                while True:
                    renewal_event.clear()
                    with renewal_lock:
                        renewed_past_deadline = any(value >= deadline for value in renewal_times)
                    if renewed_past_deadline:
                        break
                    await asyncio.wait_for(renewal_event.wait(), timeout=2)

                # Many cleaners race while the Telegram delivery coroutine remains in progress.
                recovered = await asyncio.gather(*(
                    asyncio.to_thread(limits.recover_stale_reservations, fake_now())
                    for _ in range(24)
                ))
                self.assertEqual(sum(recovered), 0)
                self.assertEqual(limits.remaining(81), limits.DAILY_LIMIT - 1)

                bot.allow_delivery.set()
                self.assertTrue(await asyncio.wait_for(task, timeout=2))
                self.assertEqual(len(bot.documents), 1)
                self.assertEqual(limits.get_usage(81)[0], 1)
                self.assertEqual(limits.remaining(81), limits.DAILY_LIMIT - 1)
            finally:
                bot.allow_delivery.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_cancellation_before_document_send_refunds_reservation(self):
        started = asyncio.Event()

        class BlockedProgressBot(FakeBot):
            async def send_message(self, chat_id, text):
                started.set()
                await asyncio.Future()

        with patch.object(limits, "RESERVATION_HEARTBEAT_INTERVAL", timedelta(hours=1)):
            task = asyncio.create_task(
                main._send_pdf(BlockedProgressBot(), 1, 82, ["text"], None)
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(limits.remaining(82), limits.DAILY_LIMIT)

    async def test_cancellation_during_document_send_keeps_lease_for_recovery(self):
        now = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
        started = asyncio.Event()

        class BlockedDeliveryBot(FakeBot):
            async def send_document(self, **_kwargs):
                started.set()
                await asyncio.Future()

        with patch.object(limits, "_now", return_value=now), \
             patch.object(limits, "RESERVATION_HEARTBEAT_INTERVAL", timedelta(hours=1)), \
             patch.object(main, "create_pdf", side_effect=lambda text, path: Path(path).write_bytes(b"pdf")):
            task = asyncio.create_task(
                main._send_pdf(BlockedDeliveryBot(), 1, 83, ["text"], None)
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(limits.remaining(83), limits.DAILY_LIMIT - 1)
            self.assertEqual(
                limits.recover_stale_reservations(now + limits.RESERVATION_TTL + timedelta(seconds=1)),
                1,
            )


if __name__ == "__main__":
    unittest.main()
