from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main


class TaskStub:
    def cancel(self):
        pass

    def done(self):
        return False


class FakeApplication:
    def create_task(self, coroutine):
        coroutine.close()
        return TaskStub()


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.edits = []
        self.deleted = False

    async def reply_text(self, text):
        self.replies.append(text)
        return self

    async def edit_text(self, text):
        self.edits.append(text)
        return self

    async def delete(self):
        self.deleted = True


class FakeBot:
    def __init__(self, fail_notice_number=None):
        self.messages = []
        self.documents = []
        self.fail_notice_number = fail_notice_number
        self.fail_delivery = False

    async def send_message(self, chat_id, text):
        if self.fail_notice_number == len(self.messages) + 1:
            raise RuntimeError("private test diagnostic; not user text")
        message = FakeMessage(text)
        self.messages.append((chat_id, text, message))
        return message

    async def send_document(self, **kwargs):
        if self.fail_delivery:
            raise RuntimeError("private delivery diagnostic")
        self.documents.append(kwargs)


class MainBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.chat = SimpleNamespace(id=700, type="private")
        self.user = SimpleNamespace(id=800, full_name="Test")
        self.bot = FakeBot()
        self.context = SimpleNamespace(bot=self.bot, application=FakeApplication(), args=[])

    def make_update(self, text, chat=None):
        message = FakeMessage(text)
        return SimpleNamespace(
            effective_chat=chat or self.chat,
            effective_user=self.user,
            effective_message=message,
            message=message,
        )

    async def asyncTearDown(self):
        for key in list(main.user_sessions):
            main._drop_session(key)

    async def test_group_text_is_rejected_before_session_or_conversion(self):
        group = SimpleNamespace(id=-100, type="group")
        update = self.make_update("DO NOT COLLECT THIS", group)
        with patch.object(main, "_send_pdf", new_callable=AsyncMock) as send_pdf:
            await main.handle_text(update, self.context)
        self.assertEqual(main.user_sessions, {})
        self.assertEqual(send_pdf.await_count, 0)
        self.assertIn("المجموعات غير مدعومة", update.effective_message.replies[0])

    async def test_14_15_16_message_boundary_and_editable_title(self):
        key = (self.chat.id, self.user.id)
        with patch.object(main, "COLLECT_DELAY", 3600), \
             patch.object(main.limits, "remaining", return_value=3), \
             patch.object(main, "_send_pdf", new_callable=AsyncMock, return_value=True) as send_pdf:
            for index in range(14):
                await main.handle_text(self.make_update(f"message {index + 1}"), self.context)
            self.assertEqual(len(main.user_sessions[key]["messages"]), 14)
            self.assertEqual(main.user_sessions[key]["state"], "collecting")

            fifteenth = self.make_update("message 15")
            await main.handle_text(fifteenth, self.context)
            self.assertEqual(len(main.user_sessions[key]["messages"]), 15)
            self.assertEqual(main.user_sessions[key]["state"], "awaiting_title")
            self.assertIn("دفتر رسائلي", fifteenth.effective_message.replies[0])

            final = self.make_update("عنوان جديد قابل للتعديل")
            await main.handle_text(final, self.context)
            send_pdf.assert_awaited_once_with(
                self.bot, self.chat.id, self.user.id,
                [f"message {i}" for i in range(1, 16)], "عنوان جديد قابل للتعديل",
            )
            self.assertNotIn(key, main.user_sessions)

    async def test_exact_telegram_message_limit_is_not_lowered(self):
        key = (self.chat.id, self.user.id)
        text = "ع" * 4096
        with patch.object(main, "COLLECT_DELAY", 3600), \
             patch.object(main.limits, "remaining", return_value=3):
            await main.handle_text(self.make_update(text), self.context)
        self.assertEqual(len(main.user_sessions[key]["messages"][0]), 4096)

    async def test_emoji_are_removed_before_pdf_collection(self):
        key = (self.chat.id, self.user.id)
        arabic_and_marks = "\uFEFBمَرْحَبًاCafe\u0301🙂world"
        self.assertEqual(main.remove_emojis(arabic_and_marks), "\uFEFBمَرْحَبًاCafe\u0301world")
        with patch.object(main, "COLLECT_DELAY", 3600), \
             patch.object(main.limits, "remaining", return_value=3):
            await main.handle_text(self.make_update("hello🙂world📄"), self.context)
        self.assertEqual(main.user_sessions[key]["messages"], ["helloworld"])

    async def test_idle_session_expires(self):
        key = (self.chat.id, self.user.id)
        main.user_sessions[key] = {
            "messages": ["draft"], "state": "awaiting_title",
            "last_activity": 100.0, "timer": TaskStub(), "token": "test",
        }
        self.assertEqual(main.cleanup_expired_sessions(now=100.0 + main.SESSION_TIMEOUT), 1)
        self.assertNotIn(key, main.user_sessions)

    async def test_pdf_conversion_concurrency_uses_separate_bounded_slots(self):
        semaphore = main._pdf_conversion_semaphore()
        active = 0
        peak = 0

        async def conversion_job():
            nonlocal active, peak
            async with semaphore:
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0)
                active -= 1

        await asyncio.gather(*(conversion_job() for _ in range(6)))
        self.assertLessEqual(peak, main.PDF_CONVERSION_CONCURRENCY)
        self.assertIsNot(semaphore, main.OUTBOUND_RATE_LIMITER)

    async def test_unsupported_group_command_is_rejected(self):
        group = SimpleNamespace(id=-99, type="supergroup")
        update = self.make_update("/skip", group)
        with patch.object(main, "_send_pdf", new_callable=AsyncMock) as send_pdf:
            await main.skip_command(update, self.context)
        self.assertEqual(send_pdf.await_count, 0)
        self.assertIn("المجموعات غير مدعومة", update.effective_message.replies[0])

    async def test_missing_token_exits_nonzero_before_application_startup(self):
        stderr = io.StringIO()
        with patch.object(main, "TOKEN", ""), patch.object(main.Application, "builder") as builder, \
             patch("sys.stderr", stderr), self.assertRaises(SystemExit) as raised:
            main.main()
        self.assertEqual(raised.exception.code, 1)
        builder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
