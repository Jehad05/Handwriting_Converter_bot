#!/usr/bin/env python3
"""Measure local PDF conversion only; this script has no Telegram client."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
from converter import create_pdf
from main import remove_emojis

TARGETS = (8_000, 16_000, 30_000, 45_000, 61_440)
MAX_MESSAGE_CHARS = 4_096
MESSAGE_COUNT = 15
PATTERNS = (
    "هذه رسالة عربية واقعية للاختبار وتحتوي على شرح واضح وتفاصيل مفيدة. ",
    "This English message contains a short explanation, practical details, and a natural sentence. ",
    "نص مختلط: موعد meeting الساعة 10:30، نسبة الإنجاز 72%، والنسخة version 2.1. ",
    "Mixed note 🙂📄: مرحبًا, page 4 of 12, topic A/B — keep punctuation and emoji. ",
)


def _repeat_to_length(pattern: str, length: int) -> str:
    if length <= 0:
        return ""
    return (pattern * ((length + len(pattern) - 1) // len(pattern)))[:length]


def build_messages(total_chars: int) -> list[str]:
    if not 0 <= total_chars <= MESSAGE_COUNT * MAX_MESSAGE_CHARS:
        raise ValueError("payload exceeds the 15 x 4096 theoretical maximum")
    base, extra = divmod(total_chars, MESSAGE_COUNT)
    lengths = [base + (1 if i < extra else 0) for i in range(MESSAGE_COUNT)]
    messages = [
        _repeat_to_length(PATTERNS[index % len(PATTERNS)], length)
        for index, length in enumerate(lengths)
    ]
    assert sum(map(len, messages)) == total_chars
    assert all(len(message) <= MAX_MESSAGE_CHARS for message in messages)
    return messages


def benchmark_case(total_chars: int) -> dict[str, int | float | str]:
    raw_messages = build_messages(total_chars)
    messages = [remove_emojis(message) for message in raw_messages]
    converter_text = "\n\n".join(messages)
    output_dir = Path(tempfile.mkdtemp(prefix="telegram-pdf-bench-"))
    output = output_dir / f"payload-{total_chars}.pdf"
    try:
        start = time.perf_counter()
        create_pdf(converter_text, output)
        elapsed = time.perf_counter() - start
        pdf_bytes = output.stat().st_size
        pages = len(PdfReader(str(output)).pages)
        return {
            "message_payload_chars": total_chars,
            "converter_input_chars_including_separators": len(converter_text),
            "emoji_chars_removed_before_conversion": sum(
                len(raw) - len(clean) for raw, clean in zip(raw_messages, messages)
            ),
            "message_count": len(messages),
            "max_single_message_chars": max(map(len, raw_messages), default=0),
            "max_single_message_chars_after_emoji_removal": max(map(len, messages), default=0),
            "elapsed_seconds": round(elapsed, 4),
            "pdf_bytes": pdf_bytes,
            "pdf_pages": pages,
            "status": "ok",
        }
    finally:
        output.unlink(missing_ok=True)
        output_dir.rmdir()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT_DIR / "benchmark-results.json")
    parser.add_argument("--targets", type=int, nargs="*", default=TARGETS)
    args = parser.parse_args()

    results = []
    for target in args.targets:
        result = benchmark_case(target)
        results.append(result)
        print(
            f"{target:>6} payload chars | {result['elapsed_seconds']:>8.4f}s | "
            f"{result['pdf_bytes']:>9} bytes | {result['pdf_pages']:>3} pages"
        )
    report = {
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "15 raw messages; local bot emoji sanitation then PDF conversion; no Telegram API calls",
        "theoretical_maximum_payload_chars": MESSAGE_COUNT * MAX_MESSAGE_CHARS,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Results saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
