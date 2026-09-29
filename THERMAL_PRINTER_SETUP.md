# Thermal Printer Setup — DHANA DHANIYA KADAI

## Quick Start

1. Install dependencies: `pip install -r backend/requirements.txt`
2. Install your thermal printer driver in Windows (generic / ESC/POS).
3. Open the app → **Admin → Printer**.
4. Select **Print Mode: ESC/POS Direct**.
5. Choose your **Windows Printer** name from the dropdown.
6. Click **Test Print**, then **Save Settings**.
7. Press **F3** or **Print** on the POS — bill prints instantly (no browser dialog).

## Print Flow

```
POS (F3 / Print)
  → POST /api/thermal/print
  → ReceiptFormatter (EN/TA monospace layout)
  → EscposGenerator (raw ESC/POS bytes)
  → ThermalPrinterService → Windows spooler or USB
  → Paper cut
```

## Configuration

| Setting | 80mm default | 58mm |
|---------|--------------|------|
| Paper width | 80mm | 58mm |
| Characters per line | 48 | 32 |
| Printable CSS width | 72mm | — |

### USB direct (optional)

If not using Windows printer name, set **USB Vendor ID** and **Product ID** (hex, e.g. `04b8` / `0202`). Find IDs in Device Manager → printer → Properties → Details → Hardware IDs.

## Tamil Receipts

- Tamil bills use the same ESC/POS pipeline with Tamil labels and product names (`name_tamil`).
- Some older thermal printers only support ASCII; if Tamil prints as boxes, use an UTF-8 capable printer or switch to English mode for printing.

## API Endpoints (testing)

| Method | URL | Purpose |
|--------|-----|---------|
| GET | `/api/printer/settings` | Load settings + printer list |
| POST | `/api/printer/settings` | Save settings |
| POST | `/api/thermal/print` | Print saved bill (silent) |
| POST | `/api/printer/test` | Sample test receipt |
| POST | `/api/printer/preview` | Text preview of a bill |
| GET | `/api/printer/sample-receipt` | Sample text layout |

## Troubleshooting

- **"No printer configured"** — Select a Windows printer in Admin → Printer.
- **Print fails** — Check `billing` logs; verify printer is online and set as default.
- **Browser dialog still appears** — Ensure mode is `escpos`, not `browser`.
- **Preview only** — Set mode to `preview` for debugging without hardware.

## QZ Tray (alternative)

1. Install [QZ Tray 2.x](https://qz.io/download/) and start it (green icon in system tray).
2. Admin → Printer → **Print Mode: QZ Tray**.
3. Set **QZ Tray Printer Name** (or leave blank to use default).
4. Click **QZ Printers** to list available names.
5. Print bills — raw ESC/POS is sent through QZ with no browser dialog.

## Reprint

- **Analytics** → open a bill → **Reprint**
- **Customer history** → 🖨️ button on each bill row

## PyInstaller

`desktop_app.spec` bundles `backend/services`, templates, static CSS, the
`backend/fonts/*.ttf` Tamil fonts, escpos hidden imports, and the native
`uharfbuzz`/`freetype` shaping libraries used for correctly-shaped Tamil
raster printing (see `backend/services/tamil_text_render.py`).

**The `dist/` folder in this repo is a stale build from before the Tamil
shaping fix and must not be used.** It contains no font files and no
uharfbuzz/freetype binaries, so Tamil printing on it will either crash
or fall back to scrambled vowel-sign ordering. Always rebuild after
pulling spec changes:

```bat
pip install -r backend\requirements.txt
pyinstaller --clean desktop_app.spec
python verify_build.py
```

`verify_build.py` checks the freshly built `dist/DhanaDhanyaKadai` folder
for the bundled fonts and native shaping libraries and fails loudly if
either is missing, instead of letting it surface later on a customer PC.

