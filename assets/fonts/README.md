# Fonts

Persian/Arabic rendering in `topdf` and `stick` needs a Unicode TTF with Arabic
glyph coverage. Drop **Vazirmatn-Regular.ttf** here:

```bash
curl -Lo assets/fonts/Vazirmatn-Regular.ttf \
  https://github.com/rastikerdar/vazirmatn/raw/master/fonts/ttf/Vazirmatn-Regular.ttf
```

Without it the bot falls back to DejaVu Sans (Latin only) or Helvetica, so
Persian text will render as boxes. Everything else keeps working.

Vazirmatn is licensed under the SIL Open Font License 1.1.
