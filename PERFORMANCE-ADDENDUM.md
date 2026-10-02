# Performance addendum

Each payload contained **15 messages of exactly 4,096 Unicode code points each** (61,440 message characters). Joining the messages adds 28 newline characters; the emoji-free test corpus was passed through equivalent `.strip()` sanitation before conversion. Inputs differed across runs, and all 16 measured payloads had unique hashes. The Arabic-dominant corpus was 80.387% Arabic letters; the mixed corpus included Arabic letters (36.654%), Latin letters (21.826%), numbers, and punctuation.

## Repeated 61,440-character conversions

Each time below covers `create_pdf` only. Each worker ran in a fresh process. Every PDF was parsed to count pages; all 10 conversions succeeded.

**Arabic-dominant** — median **2.820975 s**, maximum **2.877500 s**; all five PDFs had **18 pages**.

| Repeat | Conversion (s) | PDF bytes / per-job TMPDIR peak | Pages | Worker peak RSS (MiB) |
|---:|---:|---:|---:|---:|
| 1 | 2.820975 | 68,105 | 18 | 56.941 |
| 2 | 2.859328 | 68,073 | 18 | 56.785 |
| 3 | 2.795053 | 67,984 | 18 | 56.898 |
| 4 | 2.752546 | 67,956 | 18 | 56.934 |
| 5 | 2.877500 | 68,160 | 18 | 56.832 |

**Mixed Arabic/English/numbers/punctuation** — median **3.293949 s**, maximum **3.441270 s**; all five PDFs had **22 pages**.

| Repeat | Conversion (s) | PDF bytes / per-job TMPDIR peak | Pages | Worker peak RSS (MiB) |
|---:|---:|---:|---:|---:|
| 1 | 3.261084 | 100,555 | 22 | 57.246 |
| 2 | 3.279796 | 100,440 | 22 | 57.285 |
| 3 | 3.348825 | 100,085 | 22 | 57.328 |
| 4 | 3.441270 | 100,971 | 22 | 57.277 |
| 5 | 3.293949 | 99,922 | 22 | 57.199 |

Each process used an isolated temporary/output directory. It contained only that job’s PDF, so its peak directory use is the same byte count shown in the tables; no additional conversion temp files appeared there.

## Concurrent conversions

Batch wall time runs from worker launch through completion and includes Python startup/imports, unlike the `create_pdf` per-job times. Aggregate worker RSS is the highest sampled sum of workers’ current RSS at 10 ms intervals. Each worker’s RSS high-water mark (HWM) is reported separately.

| Batch | Jobs | Batch wall (s) | Peak sampled aggregate worker RSS (MiB) | Highest individual worker HWM (MiB) | Peak combined isolated output/TMP dirs |
|---|---:|---:|---:|---:|---:|
| 2 concurrent | 2 | 3.761104 | 113.398 | 57.340 | 168,746 bytes (0.1609 MiB) |
| 4 concurrent | 4 | 3.860132 | 226.441 | 57.273 | 335,780 bytes (0.3202 MiB) |

| Batch | Job | Case | Conversion (s) | Worker spawn-to-exit (s) | PDF bytes / per-job TMPDIR peak | Pages | Worker peak RSS (MiB) |
|---|---|---|---:|---:|---:|---:|---:|
| 2 | Arabic | Arabic-dominant | 2.871625 | 3.309395 | 68,370 | 18 | 57.031 |
| 2 | Mixed | Arabic/English/numbers/punctuation | 3.322444 | 3.760558 | 100,376 | 22 | 57.340 |
| 4 | Arabic A | Arabic-dominant | 2.956676 | 3.396873 | 68,315 | 18 | 56.906 |
| 4 | Mixed A | Arabic/English/numbers/punctuation | 3.383321 | 3.848599 | 100,165 | 22 | 57.273 |
| 4 | Arabic B | Arabic-dominant | 2.957883 | 3.395772 | 67,948 | 18 | 56.965 |
| 4 | Mixed B | Arabic/English/numbers/punctuation | 3.405248 | 3.858430 | 99,352 | 22 | 57.191 |

The concurrent disk figure is the sum of the isolated per-job directories, including their PDFs. Outputs remained present until each batch’s measurements were complete, making the completed sum the batch’s peak output footprint. The worker RSS HWM includes interpreter, dependency, and input state: immediately before conversion, serial workers were already at 56,332–56,600 KiB. Aggregate RSS is sampled and may miss a brief narrower peak; per-process HWM comes from Linux `VmHWM`, cross-checked against Python’s `resource.getrusage`.

## Environment and recommendation

Measurements ran with **Python 3.12.3** on **Ubuntu 24.04.4 LTS**, Linux kernel `6.18.38+`, x86_64, and 8 logical CPUs. No cgroup CPU or memory limit was exposed in the environment. Installed versions were `python-telegram-bot` 22.8, APScheduler 3.11.3, ReportLab 5.0.1, arabic-reshaper 3.0.1, python-bidi 0.6.11, python-dotenv 1.2.4, pypdf 6.18.1, and Pillow 12.3.0. `psutil` was not installed, but per-process RSS was measurable through Linux `/proc` and the Python standard library. No project-bundled `.ttf` fonts were present: the converter resolved its Amiri family to system Noto Naskh Arabic regular/bold and its Kalam family to system DejaVu Sans regular/bold.

**These results do not justify adding an aggregate payload cap or conversion timeout.** All five repeats of both maximum-size inputs completed, and the four-worker batch completed in 3.86 seconds of batch wall time with 226.441 MiB peak sampled aggregate worker RSS and about 0.32 MiB of output/TMP disk. Based on these measurements, retaining the existing 61,440-character theoretical payload is reasonable; no new cap or timeout is supported by the observed conversion behavior.

These repeated Arabic-dominant and mixed-corpus runs are separate workloads from the single-run record in `benchmark-results.json`, which used its own message corpus and reported 2.2682 seconds, 78,125 bytes, and 23 pages for the same raw 61,440 message characters. The different run method and text composition explain why the repeated-run page counts and timings differ; neither local benchmark is a hosting or production-performance guarantee.

This remains one offline test on a development computer, not a production capacity test. It did not start the bot, make Telegram calls, measure delivery or queue delays, or exercise sustained load, constrained deployment memory, or slow storage. Therefore the observed timings and four-worker result should not be treated as operational limits or production concurrency guarantees.

At the time of that benchmark, the converter was called directly; no Telegram client was created, no token was passed to workers, and no production source was changed during the measurement. Temporary PDFs and worker records were removed after measurement. The existing `benchmark-results.json` was preserved, and the protected source/report hashes recorded for that run matched before and after it; the later release-document updates do not change those benchmark results.
