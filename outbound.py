"""Shared, FIFO outbound pacing and conservative Telegram request retries."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import httpx
from telegram.error import NetworkError, RetryAfter
from telegram.request import HTTPXRequest, RequestData

from config import (
    BOT_GLOBAL_SENDS_PER_SECOND,
    BOT_GROUP_CHAT_INTERVAL_SECONDS,
    BOT_PRIVATE_CHAT_INTERVAL_SECONDS,
)

# Two extra attempts keep transient handling bounded while honoring RetryAfter.
MAX_RETRY_AFTER_RETRIES = 2
MAX_SAFE_CONNECT_RETRIES = 2
SAFE_CONNECT_BACKOFF_BASE_SECONDS = 0.25
RETRY_AFTER_SAFETY_PAD_SECONDS = 0.1


class DeliveryOutcomeUncertain(RuntimeError):
    """A sendDocument request may have reached Telegram, so it was not replayed."""


class OutboundRateLimiter:
    """Fair shared pacing gate for bot-wide and per-chat outgoing requests.

    Calls enter an asyncio lock in FIFO order. The lock is held only while pacing
    (never during network I/O), and deadlines are rechecked after every sleep so
    a newly received flood-control cooldown also delays callers already queued.
    """

    def __init__(
        self,
        *,
        global_sends_per_second: float = BOT_GLOBAL_SENDS_PER_SECOND,
        private_chat_interval: float = BOT_PRIVATE_CHAT_INTERVAL_SECONDS,
        group_chat_interval: float = BOT_GROUP_CHAT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        retry_after_safety_pad: float = RETRY_AFTER_SAFETY_PAD_SECONDS,
    ) -> None:
        if global_sends_per_second <= 0:
            raise ValueError("global_sends_per_second must be positive")
        if private_chat_interval < 1.0:
            raise ValueError("private chats must be paced at least one second apart")
        if group_chat_interval < 3.0:
            raise ValueError("group chats must be paced at least three seconds apart")
        self.global_interval = 1.0 / global_sends_per_second
        self.private_chat_interval = private_chat_interval
        self.group_chat_interval = group_chat_interval
        self._clock = clock
        self._sleep = sleep
        self._retry_after_safety_pad = max(0.0, retry_after_safety_pad)
        self._lock = asyncio.Lock()
        self._global_next = 0.0
        self._chat_next: dict[str, float] = {}
        self._global_cooldown_until = 0.0
        self._chat_cooldown_until: dict[str, float] = {}

    @staticmethod
    def _chat_key(chat_id: Any) -> str | None:
        return None if chat_id is None else str(chat_id)

    def _chat_interval(self, chat_id: Any) -> float:
        # Telegram group/channel chat IDs are negative integers. Named chats are
        # treated conservatively as non-private; positive numeric IDs are users.
        try:
            is_group = int(chat_id) < 0
        except (TypeError, ValueError):
            is_group = isinstance(chat_id, str) and chat_id.startswith("@")
        return self.group_chat_interval if is_group else self.private_chat_interval

    async def acquire(self, chat_id: Any = None) -> float:
        """Wait for a FIFO send slot, then return its monotonic start time."""
        key = self._chat_key(chat_id)
        async with self._lock:
            while True:
                now = self._clock()
                deadlines = [self._global_next, self._global_cooldown_until]
                if key is not None:
                    deadlines.extend((
                        self._chat_next.get(key, 0.0),
                        self._chat_cooldown_until.get(key, 0.0),
                    ))
                wait_for = max(deadlines) - now
                if wait_for > 0:
                    await self._sleep(wait_for)
                    continue

                started = self._clock()
                self._global_next = started + self.global_interval
                if key is not None:
                    self._chat_next[key] = started + self._chat_interval(chat_id)
                self._prune_chat_state(started)
                return started

    def apply_retry_after(self, retry_after: int | float, chat_id: Any = None) -> float:
        """Share Telegram's 429 cooldown with all sends and the originating chat."""
        if hasattr(retry_after, "total_seconds"):
            seconds = float(retry_after.total_seconds())
        else:
            seconds = float(retry_after)
        deadline = self._clock() + max(0.0, seconds) + self._retry_after_safety_pad
        self._global_cooldown_until = max(self._global_cooldown_until, deadline)
        key = self._chat_key(chat_id)
        if key is not None:
            self._chat_cooldown_until[key] = max(
                self._chat_cooldown_until.get(key, 0.0), deadline
            )
        return deadline

    async def sleep(self, seconds: float) -> None:
        """Use the configured clock's sleep for deterministic retry tests."""
        await self._sleep(max(0.0, seconds))

    def _prune_chat_state(self, now: float) -> None:
        # Bound long-lived per-chat state without affecting active rate windows.
        if len(self._chat_next) > 4096:
            cutoff = now - max(self.private_chat_interval, self.group_chat_interval)
            self._chat_next = {key: value for key, value in self._chat_next.items()
                               if value >= cutoff}
            self._chat_cooldown_until = {
                key: value for key, value in self._chat_cooldown_until.items()
                if value >= now
            }


def _chat_id(request_data: RequestData | None) -> Any:
    if request_data is None:
        return None
    try:
        return request_data.parameters.get("chat_id")
    except (AttributeError, TypeError):
        return None


def _is_outbound_send(endpoint: str) -> bool:
    """Identify outgoing message creation/copy/forward calls handled by the Bot API."""
    normalized = endpoint.rsplit("/", 1)[-1].lower()
    return normalized.startswith(("send", "copy", "forward"))


def _is_safe_pre_send_connection_error(error: NetworkError) -> bool:
    """Only retry failures known to occur before an HTTP request was sent."""
    cause = error.__cause__
    return isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


def _upload_rewind_points(request_data: RequestData | None) -> list[tuple[Any, int]] | None:
    """Snapshot seekable upload handles; return None if a retry cannot replay them."""
    if request_data is None:
        return []
    points: list[tuple[Any, int]] = []
    # PTB's RequestData exposes parameters for values; its InputFile collection
    # is internal, so feature-detect it and only rely on the documented InputFile
    # content interface. Normal Bot.send_document file handles are copied to bytes.
    for parameter in getattr(request_data, "_parameters", ()):
        for input_file in getattr(parameter, "input_files", None) or ():
            content = getattr(input_file, "input_file_content", None)
            if isinstance(content, (bytes, bytearray, memoryview)) or content is None:
                continue
            try:
                if not content.seekable():
                    return None
                points.append((content, content.tell()))
            except (AttributeError, OSError, ValueError):
                return None
    return points


def _restore_uploads(points: list[tuple[Any, int]] | None) -> bool:
    if points is None:
        return False
    try:
        for stream, position in points:
            stream.seek(position)
    except (OSError, ValueError):
        return False
    return True


class RateLimitedRequest(HTTPXRequest):
    """HTTPX request adapter applying one shared limiter to every outgoing send."""

    def __init__(self, *, rate_limiter: OutboundRateLimiter, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rate_limiter = rate_limiter

    async def _perform_request(self, **kwargs: Any) -> bytes:
        """Network seam, overridden in offline tests; production uses PTB HTTPX."""
        return await super()._request_wrapper(**kwargs)

    async def _request_wrapper(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = None,
        write_timeout: Any = None,
        connect_timeout: Any = None,
        pool_timeout: Any = None,
    ) -> bytes:
        endpoint = url.rsplit("/", 1)[-1]
        if method.upper() != "POST" or not _is_outbound_send(endpoint):
            return await self._perform_request(
                url=url, method=method, request_data=request_data,
                read_timeout=read_timeout, write_timeout=write_timeout,
                connect_timeout=connect_timeout, pool_timeout=pool_timeout,
            )

        chat_id = _chat_id(request_data)
        upload_points = _upload_rewind_points(request_data)
        retry_after_retries = 0
        safe_connect_retries = 0
        while True:
            await self.rate_limiter.acquire(chat_id)
            try:
                return await self._perform_request(
                    url=url, method=method, request_data=request_data,
                    read_timeout=read_timeout, write_timeout=write_timeout,
                    connect_timeout=connect_timeout, pool_timeout=pool_timeout,
                )
            except RetryAfter as exc:
                wait_seconds = exc.retry_after
                self.rate_limiter.apply_retry_after(wait_seconds, chat_id)
                can_replay = _restore_uploads(upload_points)
                if retry_after_retries >= MAX_RETRY_AFTER_RETRIES or not can_replay:
                    raise
                retry_after_retries += 1
            except NetworkError as exc:
                safe_failure = _is_safe_pre_send_connection_error(exc)
                if safe_failure and safe_connect_retries < MAX_SAFE_CONNECT_RETRIES:
                    if not _restore_uploads(upload_points):
                        # This failure is known to precede request transmission;
                        # do not label it ambiguous merely because its body cannot replay.
                        raise
                    delay = SAFE_CONNECT_BACKOFF_BASE_SECONDS * (2 ** safe_connect_retries)
                    safe_connect_retries += 1
                    await self.rate_limiter.sleep(delay)
                    continue
                if endpoint.lower() == "senddocument" and not safe_failure:
                    raise DeliveryOutcomeUncertain(
                        "document delivery outcome is uncertain; automatic replay was suppressed"
                    ) from exc
                raise
