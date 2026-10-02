"""Telegram text-to-PDF bot. Importing this module never contacts Telegram."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import sys
import tempfile
import time
import weakref
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ADMIN_USER_ID, PDF_CONVERSION_CONCURRENCY, TEMP_DIR
from converter import create_pdf
import limits
from outbound import DeliveryOutcomeUncertain, OutboundRateLimiter, RateLimitedRequest

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
COLLECT_DELAY = 5
MAX_MESSAGES = 15
SESSION_TIMEOUT = 30 * 60
SESSION_SWEEP_INTERVAL = 60

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U0001FB00-\U0001FBFF\u2600-\u26FF\u2700-\u27BF\u2300-\u23FF"
    "\u2B00-\u2BFF\U0001F1E0-\U0001F1FF\U000E0000-\U000E007F"
    "]+",
    flags=re.UNICODE,
)
_EMOJI_SELECTOR_RE = re.compile(
    r"([\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF])\uFE0F"
)
_KEYCAP_RE = re.compile(r"([#*0-9])\uFE0F?\u20E3")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# A private-chat session is isolated by both identities even though groups are rejected.
SessionKey = tuple[int, int]  # (chat_id, user_id)
user_sessions: dict[SessionKey, dict[str, Any]] = {}
OUTBOUND_RATE_LIMITER = OutboundRateLimiter()
_pdf_conversion_semaphores: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _pdf_conversion_semaphore() -> asyncio.Semaphore:
    """Keep conversion slots per event loop, independent from HTTP send pacing."""
    loop = asyncio.get_running_loop()
    semaphore = _pdf_conversion_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(PDF_CONVERSION_CONCURRENCY)
        _pdf_conversion_semaphores[loop] = semaphore
    return semaphore


def remove_emojis(text: str) -> str:
    """Remove only enumerated emoji symbols; preserve Arabic and combining marks."""
    text = _KEYCAP_RE.sub(r"\1", text)
    text = _EMOJI_SELECTOR_RE.sub(r"\1", text)
    return _EMOJI_RE.sub("", text).strip()


def private_only(handler: Callable[..., Awaitable[Any]]):
    """Refuse group/channel updates before commands or text can change bot state."""
    @wraps(handler)
    async def guarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat is None or chat.type != "private":
            message = update.effective_message
            if message is not None:
                try:
                    await message.reply_text(
                        "المجموعات غير مدعومة. من فضلك استخدم البوت في محادثة خاصة."
                    )
                except Exception as exc:
                    logger.warning("group_rejection_delivery_failed error_type=%s",
                                   type(exc).__name__)
            return None
        return await handler(update, context)
    return guarded


def _key_for(update: Update) -> SessionKey:
    if update.effective_chat is None or update.effective_user is None:
        raise ValueError("message has no private identity")
    return (int(update.effective_chat.id), int(update.effective_user.id))


def _cancel_timer(session: dict[str, Any]) -> None:
    timer = session.get("timer")
    if timer is not None and not timer.done():
        timer.cancel()
    session["timer"] = None


def _drop_session(key: SessionKey) -> dict[str, Any] | None:
    session = user_sessions.pop(key, None)
    if session:
        _cancel_timer(session)
    return session


def _active_session(key: SessionKey, now: float | None = None) -> dict[str, Any] | None:
    session = user_sessions.get(key)
    if session is None:
        return None
    now = time.monotonic() if now is None else now
    if now - session["last_activity"] >= SESSION_TIMEOUT:
        _drop_session(key)
        return None
    return session


def cleanup_expired_sessions(now: float | None = None) -> int:
    """Expire idle drafts even if their owner never sends another update."""
    now = time.monotonic() if now is None else now
    expired = [
        key for key, session in user_sessions.items()
        if now - session["last_activity"] >= SESSION_TIMEOUT
    ]
    for key in expired:
        _drop_session(key)
    return len(expired)


def _title_prompt() -> str:
    date_text = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sample = f"دفتر رسائلي — {date_text}"
    return (
        "📝 أرسل عنوانًا قابلًا للتعديل لملف PDF، أو /skip لتخطي العنوان.\n"
        f"مثال مقترح: {sample}"
    )


async def ask_title(bot, key: SessionKey, session_token: str) -> None:
    """After a short collection pause, ask for the optional editable title."""
    try:
        await asyncio.sleep(COLLECT_DELAY)
    except asyncio.CancelledError:
        return
    session = _active_session(key)
    if (session is None or session.get("state") != "collecting"
            or session.get("token") != session_token):
        return
    session["state"] = "awaiting_title"
    session["timer"] = None
    session["last_activity"] = time.monotonic()
    try:
        await bot.send_message(chat_id=key[0], text=_title_prompt())
    except Exception as exc:
        # Keep the draft collectible if the title prompt could not be delivered.
        session["state"] = "collecting"
        logger.warning("title_prompt_failed error_type=%s", type(exc).__name__)


async def _safe_failure_message(bot, chat_id: int, notify, error_ref: str,
                                delivery_uncertain: bool = False) -> None:
    if delivery_uncertain:
        text = (
            "تعذر تأكيد تسليم ملف PDF. لم تتم إعادة المحاولة تلقائيًا لتجنب إرسال "
            "ملف مكرر. تحقق من المحادثة قبل بدء تحويل جديد. "
            f"(مرجع {error_ref})"
        )
    else:
        text = f"تعذر إكمال التحويل. حاول مرة أخرى لاحقًا. (مرجع {error_ref})"
    if notify is not None:
        try:
            await notify.edit_text(text)
            return
        except Exception as exc:
            logger.warning("progress_edit_failed error_type=%s", type(exc).__name__)
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        logger.warning("failure_notice_failed error_type=%s", type(exc).__name__)


async def _reservation_heartbeat(reservation_id: int) -> None:
    """Renew a pending quota lease while conversion or Telegram delivery is active."""
    interval = limits.RESERVATION_HEARTBEAT_INTERVAL.total_seconds()
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        try:
            renewed = await asyncio.to_thread(limits.renew_reservation, reservation_id)
        except Exception as exc:
            logger.error("quota_reservation_heartbeat_failed error_type=%s",
                         type(exc).__name__)
            continue
        if not renewed:
            logger.error("quota_reservation_lease_lost")
            return


async def _stop_reservation_heartbeat(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _send_pdf(bot, chat_id: int, user_id: int, messages: list[str],
                    title: str | None) -> bool:
    """Convert and deliver; a committed quota is never refunded post-delivery."""
    if not messages:
        try:
            await bot.send_message(chat_id=chat_id, text="لا يوجد نص للتحويل.")
        except Exception as exc:
            logger.warning("empty_notice_failed error_type=%s", type(exc).__name__)
        return False

    try:
        reservation_id = limits.reserve(user_id)
    except Exception as exc:
        logger.error("quota_reservation_failed error_type=%s", type(exc).__name__)
        try:
            await bot.send_message(chat_id=chat_id, text="تعذر بدء التحويل الآن. حاول لاحقًا.")
        except Exception as send_exc:
            logger.warning("quota_error_notice_failed error_type=%s", type(send_exc).__name__)
        return False

    if reservation_id is None:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"وصلت إلى الحد اليومي ({limits.DAILY_LIMIT} تحويلات). "
                    f"يتجدد الحد عند 02:00 UTC؛ المتبقي: {limits.format_time_until_reset()}."
                ),
            )
        except Exception as exc:
            logger.warning("quota_notice_failed error_type=%s", type(exc).__name__)
        return False

    heartbeat_task = asyncio.create_task(_reservation_heartbeat(reservation_id))
    error_ref = secrets.token_hex(4)
    full_text = "\n\n".join(messages)
    if title:
        full_text = f"# {title}\n\n{full_text}"

    output_path: str | None = None
    notify = None
    delivered = False
    delivery_started = False
    delivery_uncertain = False
    stage = "progress"
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, output_path = tempfile.mkstemp(prefix="pdf-", suffix=".pdf", dir=TEMP_DIR)
        os.close(fd)
        notify = await bot.send_message(chat_id=chat_id, text="⏳ جارٍ إعداد ملف PDF...")

        stage = "conversion"
        async with _pdf_conversion_semaphore():
            await asyncio.to_thread(create_pdf, full_text, output_path)

        stage = "delivery"
        with open(output_path, "rb") as pdf_file:
            # Fence the external side effect with a fresh, owner-checked DB renewal.
            if not limits.renew_reservation(reservation_id):
                raise RuntimeError("quota reservation lease was lost before delivery")
            delivery_started = True
            await bot.send_document(
                chat_id=chat_id,
                document=pdf_file,
                filename="Notebook_A3.pdf",
                caption="تم إنشاء ملف PDF.",
            )
        delivered = True
    except asyncio.CancelledError:
        await _stop_reservation_heartbeat(heartbeat_task)
        # Before send_document begins the outcome is known; during upload it may be ambiguous.
        if not delivery_started:
            try:
                limits.refund_reservation(reservation_id)
            except Exception as refund_exc:
                logger.error("quota_refund_failed error_type=%s", type(refund_exc).__name__)
        raise
    except Exception as exc:
        await _stop_reservation_heartbeat(heartbeat_task)
        delivery_uncertain = isinstance(exc, DeliveryOutcomeUncertain)
        # Keep the quota reservation if Telegram may have accepted the document.
        if not delivered and not delivery_uncertain:
            try:
                limits.refund_reservation(reservation_id)
            except Exception as refund_exc:
                logger.error("quota_refund_failed error_type=%s", type(refund_exc).__name__)
        logger.error(
            "pdf_operation_failed ref=%s stage=%s error_type=%s delivered=%s delivery_uncertain=%s",
            error_ref, stage, type(exc).__name__, delivered, delivery_uncertain,
        )
        await _safe_failure_message(
            bot, chat_id, notify, error_ref, delivery_uncertain=delivery_uncertain
        )
        return False
    finally:
        if output_path:
            try:
                Path(output_path).unlink(missing_ok=True)
            except OSError as exc:
                # This is post-delivery housekeeping and must never trigger a refund.
                logger.warning("temporary_pdf_cleanup_failed error_type=%s", type(exc).__name__)
        if delivered:
            # Do not await here: commit the confirmed delivery before another cancellation point.
            heartbeat_task.cancel()
        else:
            await _stop_reservation_heartbeat(heartbeat_task)

    if delivered:
        commit_error = None
        committed = False
        for attempt in range(3):
            try:
                committed = limits.commit_reservation(reservation_id)
                break
            except Exception as exc:
                commit_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.05 * (attempt + 1))
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        if not committed:
            # Telegram has delivered the file; never refund due to bookkeeping failure.
            if commit_error is None:
                logger.error("quota_commit_missing ref=%s", error_ref)
            else:
                logger.error("quota_commit_failed ref=%s error_type=%s",
                             error_ref, type(commit_error).__name__)

        if notify is not None:
            try:
                await notify.delete()
            except Exception as exc:
                logger.info("progress_cleanup_failed error_type=%s", type(exc).__name__)
        try:
            left = limits.remaining(user_id)
            if left is not None:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"المحاولات المتبقية اليوم: {left}/{limits.DAILY_LIMIT}.",
                )
        except Exception as exc:
            # Remaining-quota notices happen after delivery and never refund the slot.
            logger.info("remaining_notice_failed error_type=%s", type(exc).__name__)
    return delivered


@private_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "أهلًا بك! أرسل نصوصًا وسأجمعها في ملف PDF بمقاس A3.\n\n"
        f"بعد {COLLECT_DELAY} ثوانٍ من التوقف أو عند {MAX_MESSAGES} رسائل، "
        "سأطلب عنوانًا قابلًا للتعديل (أو /skip).\n"
        "استخدم /done لإنهاء التجميع، و/cancel للإلغاء.\n"
        "يدعم المستند العربية والإنجليزية والنص المختلط. المجموعات غير مدعومة."
    )


@private_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "أرسل رسالة نصية، ويمكنك إرسال ما يصل إلى 15 رسالة في الملف الواحد. "
        "بعد الرسالة الخامسة عشرة سيُطلب منك عنوان قابل للتعديل؛ الرسالة التالية "
        "تكون العنوان. استخدم /done لإنهاء مبكرًا، /skip لتخطي العنوان، "
        "أو /cancel لإلغاء المسودة. الجلسات غير النشطة تنتهي بعد 30 دقيقة."
    )


@private_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = _key_for(update)
    session = _active_session(key)
    if session is None:
        await update.effective_message.reply_text("لا توجد مسودة نشطة.")
        return
    count = len(session["messages"])
    _drop_session(key)
    await update.effective_message.reply_text(f"أُلغيت المسودة وحُذفت {count} رسالة من الذاكرة.")


async def _convert_session(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           title: str | None) -> None:
    key = _key_for(update)
    session = _active_session(key)
    if session is None:
        return
    messages = list(session["messages"])
    _drop_session(key)
    await _send_pdf(context.bot, key[0], key[1], messages, title)


@private_only
async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = _key_for(update)
    session = _active_session(key)
    if session is None or session.get("state") != "awaiting_title":
        await update.effective_message.reply_text("لا يوجد طلب عنوان لإكماله.")
        return
    await _convert_session(update, context, title=None)


@private_only
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    key = _key_for(update)
    session = _active_session(key)
    if session is None or session.get("state") != "collecting":
        await update.effective_message.reply_text("لا توجد جلسة تجميع نشطة.")
        return
    _cancel_timer(session)
    session["state"] = "awaiting_title"
    session["last_activity"] = time.monotonic()
    await update.effective_message.reply_text(_title_prompt())


@private_only
async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    used, limit = limits.get_usage(update.effective_user.id)
    remaining = limits.remaining(update.effective_user.id)
    if limit is None:
        text = f"استخدامك اليوم: {used} تحويلات. حساب بريميوم بلا حد يومي."
    else:
        text = (
            f"استخدامك اليوم: {used}/{limit}. المتبقي: {remaining}. "
            f"يتجدد عند 02:00 UTC ({limits.format_time_until_reset()})."
        )
    await update.effective_message.reply_text(text)


@private_only
async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_USER_ID or update.effective_user.id != ADMIN_USER_ID:
        return
    try:
        target, days = int(context.args[0]), int(context.args[1])
        expiry = limits.grant_premium(target, days, granted_by=update.effective_user.id)
    except (IndexError, ValueError):
        await update.effective_message.reply_text("الاستخدام: /premium <user_id> <days>")
        return
    except Exception as exc:
        logger.error("premium_grant_failed error_type=%s", type(exc).__name__)
        await update.effective_message.reply_text("تعذر تحديث الاشتراك الآن.")
        return
    await update.effective_message.reply_text(
        f"تم تحديث الاشتراك حتى {expiry.strftime('%Y-%m-%d')} UTC."
    )


@private_only
async def unpremium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ADMIN_USER_ID or update.effective_user.id != ADMIN_USER_ID:
        return
    try:
        target = int(context.args[0])
        removed = limits.revoke_premium(target)
    except (IndexError, ValueError):
        await update.effective_message.reply_text("الاستخدام: /unpremium <user_id>")
        return
    except Exception as exc:
        logger.error("premium_revoke_failed error_type=%s", type(exc).__name__)
        await update.effective_message.reply_text("تعذر تحديث الاشتراك الآن.")
        return
    await update.effective_message.reply_text(
        "أُلغي الاشتراك." if removed else "لا يوجد اشتراك نشط لهذا المستخدم."
    )


@private_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = remove_emojis(update.effective_message.text or "")
    if not text or not text.strip():
        await update.effective_message.reply_text("أرسل نصًا غير فارغ.")
        return

    key = _key_for(update)
    session = _active_session(key)
    if session and session.get("state") == "awaiting_title":
        await _convert_session(update, context, title=text.strip())
        return

    if session is None:
        if limits.remaining(update.effective_user.id) == 0:
            await update.effective_message.reply_text(
                f"وصلت إلى الحد اليومي ({limits.DAILY_LIMIT} تحويلات). "
                f"يتجدد عند 02:00 UTC؛ المتبقي: {limits.format_time_until_reset()}."
            )
            return
        session = {
            "messages": [],
            "state": "collecting",
            "last_activity": time.monotonic(),
            "timer": None,
            "token": secrets.token_hex(8),
        }
        user_sessions[key] = session

    _cancel_timer(session)
    session["messages"].append(text)  # No bot-side cap below Telegram's 4096-character limit.
    session["last_activity"] = time.monotonic()
    count = len(session["messages"])

    if count >= MAX_MESSAGES:
        session["state"] = "awaiting_title"
        await update.effective_message.reply_text(_title_prompt())
        return

    if count > 1:
        await update.effective_message.reply_text(
            f"أُضيفت الرسالة ({count}/{MAX_MESSAGES}). أرسل المزيد أو استخدم /done."
        )

    session["timer"] = context.application.create_task(
        ask_title(context.bot, key, session["token"])
    )


async def _periodic_cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        cleanup_expired_sessions()
        limits.recover_stale_reservations()
        limits.cleanup_old()
    except Exception as exc:
        logger.error("periodic_cleanup_failed error_type=%s", type(exc).__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Do not log exception text or message text; diagnostics are deliberately minimal.
    error = context.error
    logger.error("unhandled_update_error error_type=%s", type(error).__name__)
    if isinstance(update, Update):
        chat = update.effective_chat
        message = update.effective_message
        if chat is not None and chat.type == "private" and message is not None:
            try:
                await message.reply_text("حدث خطأ غير متوقع. حاول مرة أخرى لاحقًا.")
            except Exception as exc:
                logger.warning("generic_error_notice_failed error_type=%s", type(exc).__name__)
    if ADMIN_USER_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=f"تنبيه تشغيلي: حدث خطأ غير متوقع (نوع الاستثناء {type(error).__name__}).",
            )
        except Exception as exc:
            logger.warning("admin_error_notice_failed error_type=%s", type(exc).__name__)


def main() -> None:
    if not TOKEN:
        print("BOT_TOKEN is required; set it in the project .env file.", file=sys.stderr)
        raise SystemExit(1)

    TEMP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    limits.init_db()
    limits.recover_stale_reservations()
    limits.cleanup_old()

    request = RateLimitedRequest(rate_limiter=OUTBOUND_RATE_LIMITER)
    app = Application.builder().token(TOKEN).request(request).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(CommandHandler("usage", usage_command))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("unpremium", unpremium_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    if app.job_queue is not None:
        app.job_queue.run_repeating(
            _periodic_cleanup, interval=SESSION_SWEEP_INTERVAL,
            first=SESSION_SWEEP_INTERVAL,
        )
    logger.info("bot_ready max_messages=%d idle_expiry_seconds=%d", MAX_MESSAGES, SESSION_TIMEOUT)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
