# Fonts

Persian/Arabic rendering in `topdf` and `stick` needs a Unicode TTF with Arabic
glyph coverage. **Vazirmatn-Regular.ttf** and **Vazirmatn-Bold.ttf** are bundled
here so stickers and PDFs work out of the box — no manual download needed.

If you want a different look, drop your own TTF here with the same name and it
will be picked up automatically (custom fonts are optional):

```bash
curl -Lo assets/fonts/Vazirmatn-Regular.ttf \
  https://github.com/rastikerdar/vazirmatn/raw/master/fonts/ttf/Vazirmatn-Regular.ttf
```

## License

Vazirmatn is licensed under the **SIL Open Font License 1.1** — see
[`OFL.txt`](OFL.txt). You may bundle, modify and redistribute it freely, as
long as the license file stays with the font.
