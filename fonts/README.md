# Optional fonts / الخطوط الاختيارية

The converter looks in this exact directory for `Amiri-Regular.ttf`, `Amiri-Bold.ttf`, `Kalam-Regular.ttf`, and `Kalam-Bold.ttf`. If a file is absent, it uses the compatible system fallback when available. Run `python fonts/download_fonts.py` from any working directory to download the four optional assets atomically into this directory.

الخطوط اختيارية، ومكانها المتوقع هو هذا المجلد: `Amiri-Regular.ttf` و`Amiri-Bold.ttf` و`Kalam-Regular.ttf` و`Kalam-Bold.ttf`. يستخدم المحوّل خطوط النظام البديلة عند غيابها إذا كانت متاحة. نزّل الأصول الاختيارية إلى هنا باستخدام `python fonts/download_fonts.py` من أي مجلد.

The font families are provided by Google Fonts. Check the current [Amiri](https://fonts.google.com/specimen/Amiri) and [Kalam](https://fonts.google.com/specimen/Kalam) pages and SIL Open Font License terms before redistributing font files. Font binaries are excluded from version control by default.
