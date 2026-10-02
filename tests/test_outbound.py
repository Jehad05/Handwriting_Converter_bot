from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace

import httpx
from telegram.error import NetworkError, RetryAfter

from outbound import (
    DeliveryOutcomeUncertain,
    MAX_RETRY_AFTER_RETRIES,
    OutboundRateLimiter,
    RateLimitedRequest,
    SAFE_CONNECT_BACKOFF_BASE_SECONDS,
    _is_outbound_send,
)


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class PausingClock(FakeClock):
    def __init__(self):
        super().__init__()
        self.sleep_started = asyncio.Event()
        self.release_sleep = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.sleep_started.set()
        await self.release_sleep.wait()
        self.now += seconds
        await asyncio.sleep(0)


def network_error(cause: Exception) -> NetworkError:
    error = NetworkError("mock network failure")
    error.__cause__ = cause
    return error


class MockRateLimitedRequest(RateLimitedRequest):
    def __init__(self, limiter, clock, outcomes):
        self.clock = clock
        self.outcomes = list(outcomes)
        self.calls: list[tuple[float, str]] = []
        super().__init__(rate_limiter=limiter)

    async def _perform_request(self, **kwargs):
        endpoint = kwargs["url"].rsplit("/", 1)[-1]
        self.calls.append((self.clock.now, endpoint))
        if not self.outcomes:
            return b"ok"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class OutboundLimiterTests(unittest.IsolatedAsyncioTestCase):
    def make_limiter(self, clock, *, global_rate=20.0, chat_interval=1.0):
        return OutboundRateLimiter(
            global_sends_per_second=global_rate,
            private_chat_interval=chat_interval,
            group_chat_interval=3.05,
            clock=lambda: clock.now,
            sleep=clock.sleep,
            retry_after_safety_pad=0.1,
        )

    async def test_fifo_queue_order_and_per_chat_pacing(self):
        clock = FakeClock()
        limiter = self.make_limiter(clock, global_rate=20.0)
        starts: list[tuple[str, float]] = []

        async def send(label: str) -> None:
            started = await limiter.acquire(12345)
            starts.append((label, started))

        tasks = [asyncio.create_task(send(label)) for label in ("first", "second", "third")]
        await asyncio.gather(*tasks)
        self.assertEqual([label for label, _ in starts], ["first", "second", "third"])
        self.assertEqual([round(start, 3) for _, start in starts], [0.0, 1.0, 2.0])

    async def test_bot_wide_pacing_applies_across_distinct_chats(self):
        clock = FakeClock()
        limiter = self.make_limiter(clock, global_rate=10.0)
        starts: list[float] = []

        async def send(chat_id: int) -> None:
            starts.append(await limiter.acquire(chat_id))

        await asyncio.gather(*(send(chat_id) for chat_id in (100, 101, 102)))
        self.assertEqual([round(start, 3) for start in starts], [0.0, 0.1, 0.2])

    async def test_group_chat_pacing_respects_twenty_per_minute_guidance(self):
        clock = FakeClock()
        limiter = self.make_limiter(clock, global_rate=100.0)
        first = await limiter.acquire(-100123)
        second = await limiter.acquire(-100123)
        self.assertEqual(round(first, 2), 0.0)
        self.assertEqual(round(second - first, 2), 3.05)

    async def test_private_message_and_document_use_the_same_request_limiter(self):
        clock = FakeClock()
        limiter = self.make_limiter(clock)
        request = MockRateLimitedRequest(clock=clock, limiter=limiter, outcomes=[b"message", b"document"])
        for endpoint in ("sendMessage", "sendDocument"):
            await request._request_wrapper(
                url=f"https://api.telegram.org/botTEST/{endpoint}",
                method="POST",
                request_data=SimpleNamespace(parameters={"chat_id": 5}, _parameters=[]),
            )
        self.assertEqual(request.calls, [(0.0, "sendMessage"), (1.0, "sendDocument")])

    async def test_retry_after_waits_exact_server_delay_plus_small_pad(self):
        clock = FakeClock()
        limiter = self.make_limiter(clock)
        request = MockRateLimitedRequest(
            clock=clock, limiter=limiter, outcomes=[RetryAfter(2), b"ok"]
        )
        result = await request._request_wrapper(
            url="https://api.telegram.org/botTEST/sendDocument",
            method="POST",
            request_data=SimpleNamespace(parameters={"chat_id": 8}, _parameters=[]),
        )
        self.assertEqual(result, b"ok")
        self.assertEqual([round(t, 2) for t, _ in request.calls], [0.0, 2.1])
        self.assertGreaterEqual(sum(clock.sleeps), 2.1)

    async def test_429_cooldown_is_shared_with_sends_already_queued(self):
        clock = PausingClock()
        limiter = self.make_limiter(clock)
        request = MockRateLimitedRequest(
            clock=clock, limiter=limiter, outcomes=[RetryAfter(timedelta(seconds=2)), b"retry", b"queued"]
        )

        async def send(endpoint: str, chat_id: int):
            return await request._request_wrapper(
                url=f"https://api.telegram.org/botTEST/{endpoint}",
                method="POST",
                request_data=SimpleNamespace(parameters={"chat_id": chat_id}, _parameters=[]),
            )

        retrying = asyncio.create_task(send("sendMessage", 11))
        await clock.sleep_started.wait()
        queued = asyncio.create_task(send("sendDocument", 12))
        await asyncio.sleep(0)
        clock.release_sleep.set()
        await asyncio.gather(retrying, queued)

        self.assertEqual([endpoint for _, endpoint in request.calls], [
            "sendMessage", "sendMessage", "sendDocument"
        ])
        self.assertGreaterEqual(request.calls[1][0], 2.1)
        self.assertGreaterEqual(request.calls[2][0], request.calls[1][0])

    async def test_repeated_429_retries_are_bounded_and_still_set_cooldown(self):
        clock = FakeClock()
        limiter = self.make_limiter(clock)
        request = MockRateLimitedRequest(
            clock=clock, limiter=limiter,
            outcomes=[RetryAfter(1), RetryAfter(1), RetryAfter(1), b"not reached"],
        )
        with self.assertRaises(RetryAfter):
            await request._request_wrapper(
                url="https://api.telegram.org/botTEST/sendMessage",
                method="POST",
                request_data=SimpleNamespace(parameters={"chat_id": 21}, _parameters=[]),
            )
        self.assertEqual(len(request.calls), MAX_RETRY_AFTER_RETRIES + 1)
        self.assertGreaterEqual(request.calls[-1][0], 2.2)
        # The final 429 also cools down later sends even though this request stops retrying.
        later = await limiter.acquire(99)
        self.assertGreaterEqual(later, 3.3)

    async def test_only_proven_pre_send_connect_errors_use_exponential_retry(self):
        clock = FakeClock()
        limiter = self.make_limiter(clock)
        request = MockRateLimitedRequest(
            clock=clock, limiter=limiter,
            outcomes=[network_error(httpx.ConnectError("offline")), b"ok"],
        )
        await request._request_wrapper(
            url="https://api.telegram.org/botTEST/sendMessage",
            method="POST",
            request_data=SimpleNamespace(parameters={"chat_id": 31}, _parameters=[]),
        )
        self.assertEqual(len(request.calls), 2)
        self.assertGreaterEqual(request.calls[1][0], SAFE_CONNECT_BACKOFF_BASE_SECONDS)

    async def test_ambiguous_document_network_failure_is_not_retried(self):
        clock = FakeClock()
        limiter = self.make_limiter(clock)
        request = MockRateLimitedRequest(
            clock=clock, limiter=limiter,
            outcomes=[network_error(httpx.ReadTimeout("response timed out")), b"must not send twice"],
        )
        with self.assertRaises(DeliveryOutcomeUncertain):
            await request._request_wrapper(
                url="https://api.telegram.org/botTEST/sendDocument",
                method="POST",
                request_data=SimpleNamespace(parameters={"chat_id": 41}, _parameters=[]),
            )
        self.assertEqual(len(request.calls), 1)

    async def test_safe_document_connection_failure_may_retry(self):
        clock = FakeClock()
        limiter = self.make_limiter(clock)
        request = MockRateLimitedRequest(
            clock=clock, limiter=limiter,
            outcomes=[network_error(httpx.ConnectError("connection refused")), b"ok"],
        )
        await request._request_wrapper(
            url="https://api.telegram.org/botTEST/sendDocument",
            method="POST",
            request_data=SimpleNamespace(parameters={"chat_id": 42}, _parameters=[]),
        )
        self.assertEqual(len(request.calls), 2)

    async def test_send_endpoint_classification_covers_message_creation_variants(self):
        for endpoint in ("sendMessage", "sendDocument", "sendMediaGroup", "copyMessage", "forwardMessage"):
            self.assertTrue(_is_outbound_send(endpoint), endpoint)
        for endpoint in ("getUpdates", "editMessageText", "deleteMessage"):
            self.assertFalse(_is_outbound_send(endpoint), endpoint)


if __name__ == "__main__":
    unittest.main()
