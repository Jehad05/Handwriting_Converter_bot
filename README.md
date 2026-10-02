# Telegram PDF Notebook Bot (English)

A private-chat Telegram bot that turns text messages into an A3 notebook PDF with a gold frame, blue text, and Arabic, English, and mixed-script support. This repository contains source code and tests; it does not include a token, database, PDF, or binary font files.

## Implemented

- **Private chats only:** Group and channel updates are rejected before content collection or command handling. Group content is neither collected nor converted.
- **Collection and title:** The bot collects up to 15 text messages and asks for an editable title after message 15 or after five seconds of inactivity. `/done` asks for a title early, `/skip` omits it, and the first text message after a title prompt is used as the title. The suggested title is `دفتر رسائلي — [تاريخ الإنشاء]`.
- **Limits:** The bot adds no lower per-message limit than Telegram's usual 4,096-character limit and does not truncate text. The theoretical payload maximum is **15 × 4,096 = 61,440 characters**, before separators and an optional title. The same enumerated emoji ranges as the existing behavior are removed before rendering; Arabic marks and combining characters outside those ranges remain.
- **Expiry and commands:** A draft expires after 30 minutes of inactivity. `/start` and `/help` show instructions; `/cancel` discards the in-memory draft; `/usage` reports usage. `/premium` and `/unpremium` are restricted to the configured admin ID.
- **Quota and reservation:** The default limit is 3 conversions per user in a daily window beginning at **02:00 UTC**. SQLite `BEGIN IMMEDIATE` transactions serialize reservations and cleanup, and pending reservations count against quota. A reservation heartbeat renews every five minutes; abandoned reservations are recovered only after more than two hours without a heartbeat (or from `reserved_at` for the older schema). Known pre-delivery failures refund the slot. Cancellation or network failure after document sending begins can have an ambiguous outcome, so the reservation is retained rather than immediately refunded.
- **Delivery and pacing:** Outgoing messages and documents share one in-process FIFO limiter: 20 requests per second bot-wide by default, at least one second between private-chat sends, and 3.05 seconds between group sends. On 429, the limiter applies Telegram's `retry_after` plus 0.1 seconds to the global queue and affected chat, with at most two additional retries. Exponential retries are limited to known pre-send connection failures, with two additional attempts after 0.25 and 0.5 seconds. If PDF delivery is ambiguous, **the bot does not automatically resend the document**; it asks the user to check the chat.
- **Data and temporary files:** Message and title text are not written to application logs. Temporary PDFs use unique names and are removed on the normal cleanup path; a forced process kill can leave a temporary file behind. `limits.py` enforces mode `0700` only on the app-owned `DATA_DIR`; a shared or sticky `DATA_DIR` is rejected. It enforces mode `0600` on the database and its `-wal`, `-shm`, and `-journal` files. A custom `DB_PATH` outside `DATA_DIR` is accepted only when its existing parent is already a user-owned `0700` directory; shared paths such as `/tmp` are rejected without changing their permissions.
- **Paths:** Relative `DATA_DIR`, `DB_PATH`, and `TEMP_DIR` values are resolved relative to the project directory, not the current working directory.

## Not yet tested / operational gates

The passing suite below is local and uses temporary SQLite databases, fake clocks, and mocked Telegram delivery. The bot was not started, no real token was used, and no live Telegram API endpoint was contacted; the project was not tested on a hosting provider. Before deployment, run an end-to-end test with two users, verify an Arabic font is available and the generated PDF renders correctly on the target host, validate token and permissions in the operator-controlled environment, and test against live Telegram while observing target hardware and storage limits.

Collection drafts live in process memory and are lost when the process restarts. The limiter is process-local; quota reservations coordinate only through the same local SQLite database, not separate database copies. Run **exactly one process per bot token**. Even with a shared database, Telegram document delivery and the SQLite commit cannot participate in one atomic transaction. If the process or event loop pauses for more than two hours while an upload is in flight, its reservation can expire while Telegram is still processing the request; a late delivery cannot be recalled or atomically matched to SQLite. The two-hour interval is stale-reservation recovery, not a guarantee against this race.

### Fonts on minimal systems

ReportLab needs a usable Arabic font and a usable Latin font. On minimal systems without suitable fonts, **install an Arabic font** (for example Noto Naskh Arabic or Amiri) and a Latin font such as DejaVu Sans, or provide all four optional files in `fonts/`: `Amiri-Regular.ttf`, `Amiri-Bold.ttf`, `Kalam-Regular.ttf`, and `Kalam-Bold.ttf`. The converter checks the known system paths in `converter.py` and raises an error if it cannot find usable Arabic and Latin fonts. Optional files can be downloaded with `python fonts/download_fonts.py`; the ZIP excludes downloaded fonts. Check font licenses before redistribution.

## Install and configure

Python 3.10 or newer is required. The current local suite was run with Python 3.12.3 on Ubuntu 24.04.4 LTS. From the project directory:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set `BOT_TOKEN` in `.env` before running. Do not put `.env` or any token in the ZIP or version control. If `BOT_TOKEN` is missing, `main.py` exits non-zero before creating the Telegram application. An admin is optional; `ADMIN_USER_ID=0` disables Premium management commands and operational admin notices.

Optional settings are `DAILY_LIMIT` (positive integer, default 3), `DATA_DIR`, `DB_PATH`, `TEMP_DIR`, `BOT_GLOBAL_SENDS_PER_SECOND` (1–25, default 20), `BOT_PRIVATE_CHAT_INTERVAL_SECONDS` (default 1; values below 1 are rejected), `BOT_GROUP_CHAT_INTERVAL_SECONDS` (default 3.05; values below 3 are rejected), and `PDF_CONVERSION_CONCURRENCY` (positive integer, default 2). Invalid values fall back to defaults where applicable. Rate pacing is per process and does not coordinate across processes.

## Run and commands

After configuring the token and fonts:

```bash
python main.py
```

| Command | Purpose |
|---|---|
| `/start`, `/help` | Instructions |
| `/done` | Finish collection and request a title now |
| `/skip` | Create the PDF without an extra title after the title prompt |
| `/cancel` | Discard the in-memory draft |
| `/usage` | Show daily quota usage |
| `/premium <user_id> <days>` | Grant/extend Premium; admin only |
| `/unpremium <user_id>` | Revoke Premium; admin only |

## Offline tests

From the project root, run the unit suite without starting polling or contacting Telegram:

```bash
python3 -m unittest discover -s tests -v
```

The current suite contains **43 tests**, counted from the verbose run: `test_delivery.py` 10, `test_limits.py` 12, `test_main.py` 8, `test_outbound.py` 11, and `test_pdf.py` 2. They cover session/title/private-chat behavior, quota and reservations (including SQLite permission checks), schema migration, cleanup, PDF rendering, send ordering, 429 handling, and suppression of retries when PDF delivery is ambiguous. `python3 -m compileall -q .` and `python3 -m pip check` also passed locally. The separate `benchmark-results.json` record for a 61,440-message-character payload (61,141 converter-input characters after sanitation and separators) reports 2.2682 seconds, a 78,125-byte PDF, and 23 pages on this development computer. It is a converter-only local measurement, not a hosting or production-performance guarantee.

## Dependencies and licenses

Python dependencies and pinned versions are listed in `requirements.txt`. The project uses the standard MIT license in `LICENSE`; fill in `[YEAR]` and `[COPYRIGHT HOLDER]` yourself before redistribution. **The MIT license does not automatically cover Amiri or Kalam fonts, Python dependencies, or operating-system components.** Fonts and dependencies have separate terms; review their current licenses before redistribution.

---

# Telegram PDF Notebook Bot

بوت تيليجرام خاص يحوّل الرسائل النصية إلى دفتر PDF بمقاس A3، بإطار ذهبي وحبر أزرق ودعم للعربية والإنجليزية والنص المختلط. هذا المستودع يحتوي الشيفرة والاختبارات؛ لا يتضمن توكنًا أو قاعدة بيانات أو ملفات PDF أو ملفات خطوط ثنائية.

## ما نُفذ

- **المحادثات الخاصة فقط:** تُرفض تحديثات المجموعات والقنوات قبل جمع النص أو تنفيذ الأمر؛ لا يُجمع محتوى المجموعات ولا يُحوّل.
- **الجمع والعنوان:** تُجمع حتى 15 رسالة نصية، وتظهر مطالبة بعنوان قابل للتعديل بعد الرسالة الخامسة عشرة أو بعد خمس ثوانٍ من التوقف عن الكتابة. `/done` يطلب العنوان مبكرًا، و`/skip` يتخطاه، وأول رسالة نصية بعد طلب العنوان تصبح العنوان. يقترح البوت `دفتر رسائلي — [تاريخ الإنشاء]`.
- **الحدود:** لا يفرض البوت حدًا أقل من حد Telegram المعتاد البالغ 4096 محرفًا للرسالة ولا يقتطع النص. الحد النظري هو **15 × 4096 = 61,440 محرفًا** قبل فواصل السطور والعنوان. تُزال رموز emoji المحددة قبل التحويل كما في السلوك الأصلي؛ تبقى علامات التشكيل العربية والحروف وعلامات الجمع غير المشمولة في نطاقات الإزالة.
- **انتهاء المسودة والأوامر:** تنتهي المسودة بعد 30 دقيقة من عدم النشاط. `/start` و`/help` للتعليمات، و`/cancel` لإلغاء المسودة من الذاكرة، و`/usage` لعرض الاستخدام. `/premium` و`/unpremium` متاحان فقط لمعرّف الأدمن المضبوط.
- **الحصة والحجز:** الحد الافتراضي 3 تحويلات لكل مستخدم في نافذة يومية تبدأ عند **02:00 UTC**. تستخدم SQLite معاملات `BEGIN IMMEDIATE` للحجز والتنظيف، وتُحسب الحجوزات المعلقة ضمن الحصة. يتجدد نبض الحجز كل خمس دقائق؛ ويُستعاد الحجز بعد مرور أكثر من ساعتين على آخر نبض (أو من `reserved_at` للمخطط القديم). يعيد الفشل المعروف قبل التسليم الحصة، لكن الإلغاء أو فشل الشبكة بعد بدء إرسال المستند قد يترك النتيجة ملتبسة، وعندئذ لا يُعاد الحجز فورًا.
- **التسليم والإرسال:** تمر رسائل الإرسال والمستندات عبر محدد FIFO واحد داخل العملية: الافتراضي 20 طلبًا في الثانية إجمالًا، وفاصل ثانية على الأقل للمحادثة الخاصة و3.05 ثانية للمجموعة. عند 429 يطبق `retry_after` مع هامش 0.1 ثانية على الطابور العام والمحادثة، حتى محاولتين إضافيتين. تقتصر إعادة المحاولة الآمنة على أعطال اتصال ثبت أنها قبل إرسال الطلب، بمحاولتين إضافيتين بعد 0.25 و0.5 ثانية. عند غموض نتيجة رفع PDF **لا يعيد البوت الملف تلقائيًا**؛ يخطر المستخدم ليتحقق من المحادثة.
- **خصوصية البيانات والملفات المؤقتة:** نص الرسائل والعناوين لا تُكتب في السجل. ملفات PDF المؤقتة ذات أسماء فريدة وتُنظف في مسار التنظيف المعتاد؛ وقد يبقى ملف مؤقت عند قتل العملية قسرًا. يفرض `limits.py` صلاحية `0700` على `DATA_DIR` المملوك للتطبيق فقط؛ ويُرفض `DATA_DIR` المشترك أو الذي يحمل sticky bit. كما يفرض صلاحية `0600` على ملف القاعدة وملفات `-wal` و`-shm` و`-journal`. لا يُقبل `DB_PATH` مخصص خارج `DATA_DIR` إلا إذا كان مجلده الأب موجودًا ومملوكًا للمستخدم وصلاحيته `0700`؛ تُرفض المسارات المشتركة مثل `/tmp` دون تغيير أذوناتها.
- **المسارات:** المسارات النسبية لـ`DATA_DIR` و`DB_PATH` و`TEMP_DIR` تُحل نسبةً إلى مجلد المشروع، لا إلى مجلد التشغيل الحالي.

## ما لم يُختبر بعد / بوابات التشغيل

نجاح الاختبارات أدناه تحقق محليًا وباستخدام قواعد SQLite مؤقتة وساعات وهمية وتسليم Telegram محاكى. لم يُشغّل البوت، ولم يُستخدم توكن حقيقي، ولم يُتصل بواجهة Telegram حيّة، ولم يُختبر على استضافة. تبقى بوابات ما قبل التشغيل: اختبار end-to-end لمستخدمين اثنين، والتحقق من وجود خط عربي صالح ومخرجات PDF على الاستضافة المستهدفة، والتحقق من التوكن والأذونات، وتجربة ممتدة مع Telegram الحقيقي ومراقبة موارد الخادم.

توجد مسودات الجمع في ذاكرة العملية وتضيع عند إعادة تشغيلها. محدد الإرسال داخل العملية، والحجوزات تُنسق فقط عبر قاعدة SQLite المحلية نفسها، لا عبر نسخ قواعد منفصلة. شغّل **نسخة واحدة فقط لكل توكن**. حتى عند مشاركة قاعدة واحدة، لا يشترك إرسال Telegram وتحديث SQLite في معاملة ذرية: إذا توقفت العملية أو حلقة الأحداث لأكثر من ساعتين أثناء رفع بدأ بالفعل، فقد ينتهي الحجز بينما يظل طلب Telegram قيد التنفيذ؛ لا يمكن سحب التسليم المتأخر أو جعله ذريًا مع SQLite. الساعتان حد لاستعادة الحجز المتروك وليستا ضمانًا ضد السباق.

### الخطوط على الأنظمة الدنيا

يحتاج ReportLab إلى خط عربي صالح وخط لاتيني صالح. على الأنظمة الدنيا التي لا تتضمن خطوطًا مناسبة، **يجب تثبيت خط عربي** (مثل Noto Naskh Arabic أو Amiri) وخط لاتيني مثل DejaVu Sans، أو توفير الخطوط الاختيارية الأربعة في `fonts/`: `Amiri-Regular.ttf`, `Amiri-Bold.ttf`, `Kalam-Regular.ttf`, و`Kalam-Bold.ttf`. يستخدم المحوّل مسارات خطوط النظام المعروفة في `converter.py`، ويخرج بخطأ إذا لم يجد خطًا عربيًا ولاتينيًا صالحين. يمكن تنزيل الملفات الاختيارية بواسطة `python fonts/download_fonts.py`؛ لا يحتوي ZIP على هذه الخطوط المنزّلة. تحقق من ترخيص الخطوط قبل توزيعها.

## التثبيت والإعداد

يتطلب Python 3.10 أو أحدث. شُغّلت مجموعة الاختبارات الحالية محليًا على Python 3.12.3 وUbuntu 24.04.4 LTS. من مجلد المشروع:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

أدخل توكن البوت في `BOT_TOKEN` داخل `.env` قبل التشغيل. لا تضع `.env` أو أي توكن في ZIP أو نظام تحكم بالإصدارات. إذا غاب `BOT_TOKEN`، يخرج `main.py` برمز فشل غير صفري قبل إنشاء تطبيق Telegram. لا يلزم أدمن؛ `ADMIN_USER_ID=0` يعطل أوامر إدارة Premium والتنبيه التشغيلي.

المتغيرات الاختيارية: `DAILY_LIMIT` (عدد موجب، الافتراضي 3)، و`DATA_DIR` و`DB_PATH` و`TEMP_DIR`، و`BOT_GLOBAL_SENDS_PER_SECOND` (1–25، الافتراضي 20)، و`BOT_PRIVATE_CHAT_INTERVAL_SECONDS` (الافتراضي 1؛ لا يقبل أقل من 1)، و`BOT_GROUP_CHAT_INTERVAL_SECONDS` (الافتراضي 3.05؛ لا يقبل أقل من 3)، و`PDF_CONVERSION_CONCURRENCY` (عدد موجب، الافتراضي 2). القيم غير الصالحة تعود إلى الافتراضيات حيث ينطبق ذلك. إعداد التهدئة خاص بكل عملية ولا ينسق بين العمليات.

## التشغيل والأوامر

بعد إعداد التوكن والخطوط:

```bash
python main.py
```

| الأمر | الغرض |
|---|---|
| `/start`, `/help` | التعليمات |
| `/done` | إنهاء الجمع وطلب العنوان الآن |
| `/skip` | إنشاء PDF دون عنوان إضافي بعد مطالبة العنوان |
| `/cancel` | إلغاء المسودة من الذاكرة |
| `/usage` | عرض استخدام الحصة اليومية |
| `/premium <user_id> <days>` | منح/تمديد Premium للأدمن فقط |
| `/unpremium <user_id>` | إلغاء Premium للأدمن فقط |

## الاختبارات المتاحة دون Telegram

من جذر المشروع، شغّل مجموعة اختبارات الوحدة دون بدء الاستطلاع أو الاتصال بواجهة Telegram:

```bash
python3 -m unittest discover -s tests -v
```

المجموعة الحالية **43 اختبارًا** وفق خرج التشغيل المفصل: `test_delivery.py` عددها 10، و`test_limits.py` عددها 12، و`test_main.py` عددها 8، و`test_outbound.py` عددها 11، و`test_pdf.py` عددها 2. تغطي حدود الجلسات والعنوان والخصوصية والحصة والحجز (بما فيها صلاحيات SQLite)، والترحيل والتنظيف والتحويل وترتيب الإرسال و429 وعدم إعادة إرسال PDF ذي النتيجة الملتبسة. نجح أيضًا `python3 -m compileall -q .` و`python3 -m pip check`. يسجل `benchmark-results.json` تجربة محلية منفصلة بحمولة رسائل 61,440 محرفًا (61,141 محرف إدخال للمحوّل بعد التنظيف والفواصل): 2.2682 ثانية وPDF بحجم 78,125 بايت و23 صفحة. هذه نتيجة محوّل محلية واحدة وليست ضمانًا للأداء على استضافة أخرى.

## الاعتماديات والتراخيص

اعتماديات Python وإصداراتها مثبتة في `requirements.txt`. ترخيص المشروع القياسي هو MIT في `LICENSE`؛ املأ خانتي `[YEAR]` و`[COPYRIGHT HOLDER]` بنفسك قبل إعادة التوزيع. **ترخيص MIT لا يمنح تلقائيًا حقوقًا على خطوط Amiri أو Kalam أو الاعتماديات أو مكونات النظام.** لكل خط واعتمادية شروط منفصلة؛ راجع تراخيصها الحالية قبل التوزيع.
