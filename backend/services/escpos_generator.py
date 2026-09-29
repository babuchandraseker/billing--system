"""
ESC/POS byte generator — consumes pre-formatted lines from receipt_formatter only.

SINGLE SOURCE OF TRUTH: This module NEVER reformats, truncates, strips centering
spaces, or re-aligns text. The receipt_formatter owns all layout. This module
only converts formatted_lines[] to raw ESC/POS bytes.

FONT MODE — 80mm paper width
─────────────────────────────────
Font A (12×24 dots): 576 / 12 = 48 columns  ← active (80mm paper)
Font B  (9×17 dots): 576 /  9 = 64 columns  ← narrower glyphs, not used

The ESC M command selects the font:
  ESC M 0  → Font A  (48 cols on 80mm)  ← active
  ESC M 1  → Font B  (64 cols on 80mm)

WIDTH = 48 matches receipt_formatter.ENGLISH_WIDTH = TAMIL_WIDTH = 48.
If WIDTH != 48, ESC/POS output will clip Qty/Amt columns silently.

To switch to 58mm paper:
  Font B (42 cols): FONT_MODE_BYTE = b'\\x1b\\x4d\\x01', WIDTH = 42
  Font A (32 cols): FONT_MODE_BYTE = b'\\x1b\\x4d\\x00', WIDTH = 32
"""

import logging
import os

logger = logging.getLogger('billing.printer')

# ── Font selection ────────────────────────────────────────────────────────────
# ESC M 0 = Font A (12×24 dots) — 80mm: 576/12 = 48 cols  ← active
# ESC M 1 = Font B  (9×17 dots) — 80mm: 576/9  = 64 cols
FONT_MODE_BYTE = b'\x1b\x4d\x00'   # ESC M 0 → Font A, 48 columns on 80mm paper

# Safety-net clip width — MUST match receipt_formatter.ENGLISH_WIDTH / TAMIL_WIDTH (48).
# WARNING: If this does not equal 48, lines will be truncated and Qty/Amt columns
# will be silently dropped from ESC/POS and QZ Tray output.
WIDTH = 48  # 80mm paper, Font A: 576 dots / 12 = 48 columns


class EscposGenerator:
    """Convert receipt text lines to ESC/POS commands via python-escpos."""

    def __init__(self, settings=None):
        self.settings = settings or {}

    def _reset_style(self, p):
        """Reset to base style: Font A, bold ON, left-aligned, no scaling.
        Note: bold=True here keeps double-strike effective after p.set() calls.
        ESC E 1 (double-strike) is set independently in _set_density/_inject_font_a.
        """
        try:
            p.set(
                align='left',
                font='a',          # Font A — matches FONT_MODE_BYTE (48 cols on 80mm)
                bold=True,         # Keep bold ON always — thermal printers need it for darkness
                double_height=False,
                double_width=False,
            )
        except Exception:
            pass

    def _set_density(self, p):
        """Set maximum print density for darkest possible thermal output."""
        try:
            p.set(density=9)   # escpos-python density: 0–8 range; 9 = max saturate
        except Exception:
            pass
        # ESC/POS raw: ESC E 1 = emphasize (double-strike) ON
        # This doubles the ink pass on most thermal printers → significantly darker.
        try:
            if hasattr(p, '_raw'):
                p._raw(b'\x1b\x45\x01')  # ESC E 1 — emphasize ON
        except Exception:
            pass

    def _clear_emphasis(self, p):
        """Turn off double-strike to reset between special lines (optional)."""
        try:
            if hasattr(p, '_raw'):
                p._raw(b'\x1b\x45\x00')  # ESC E 0 — emphasize OFF
        except Exception:
            pass

    def _inject_font_a(self, p):
        """Send ESC M 0 raw bytes to activate Font A (48 cols on 80mm paper)."""
        try:
            if hasattr(p, '_raw'):
                p._raw(FONT_MODE_BYTE)
        except Exception:
            pass

    def _line_width(self):
        return WIDTH

    def _is_thin_separator(self, text):
        s = (text or '').strip()
        return bool(s) and len(set(s)) == 1 and s[0] == '-'

    def _is_bold_separator(self, text):
        s = (text or '').strip()
        return bool(s) and len(set(s)) == 1 and s[0] == '='

    def _is_total_line(self, text):
        s = (text or '').strip()
        if not s:
            return False
        u = s.upper()
        return u.startswith('TOTAL') or s.startswith('மொத்தம்')

    def _is_nutrition_title(self, text):
        s = (text or '').strip().upper()
        return 'NUTRITION' in s or 'ஊட்டச்சத்து' in (text or '')

    def _is_thanks_line(self, text):
        s = (text or '').strip()
        if not s:
            return False
        # Heart symbol replaced by '*' in ESC/POS output; detect both forms.
        return '♥' in s or '*' in s or 'THANK YOU' in s.upper() or 'நன்றி' in s

    def _unicode_font_path(self):
        """Find a font with Tamil glyphs for raster thermal receipts.

        Priority:
          1. Admin-configured path (printer settings)
          2. Bundled NotoSansTamil-Regular.ttf  <- always present, works offline
          3. Windows system fonts (Nirmala, Latha) - fallback only
        """
        configured = (self.settings.get('unicode_font_path') or '').strip()

        # Bundled font: backend/fonts/NotoSansTamil-Regular.ttf
        # Path is relative to this file (services/escpos_generator.py -> ../fonts/)
        _here = os.path.dirname(os.path.abspath(__file__))
        bundled = os.path.normpath(os.path.join(_here, '..', 'fonts', 'NotoSansTamil-Regular.ttf'))

        from .tamil_text_render import bundled_font_dirs
        candidates = [
            configured,
            *[os.path.join(d, 'NotoSansTamil-Regular.ttf') for d in bundled_font_dirs()],  # dev + PyInstaller
            bundled,   # <- bundled Noto Sans Tamil, works on all machines offline
            os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'Nirmala.ttc'),
            os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'Nirmala.ttf'),
            os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'Latha.ttf'),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                logger.info('Tamil raster font selected: %s', path)
                return path
        raise RuntimeError(
            'Tamil thermal printing requires a Tamil font. '
            'Expected bundled font at: ' + bundled
        )

    def generate_image_raster_bytes(self, image):
        """Convert a PIL image directly into ESC/POS bitImageRaster command stream."""
        from escpos.printer import Dummy
        p = Dummy()
        self._reset_style(p)
        self._set_density(p)
        p.image(image, impl='bitImageRaster', center=False)
        if self.settings.get('auto_cut', True):
            try:
                p.cut()
            except Exception:
                pass
        return p.output if hasattr(p, 'output') else p.get_output()

    def _generate_raster_receipt_bytes(self, lines, ctx=None):
        """Draw Tamil receipt text as an image because ESC/POS codepages lack Tamil.

        Uses TamilReceiptRasterRenderer with pixel-anchored layout when ctx is provided.
        """
        from PIL import Image
        from .tamil_text_render import ShapedFontRenderer, TamilReceiptRasterRenderer

        paper_width = int(self.settings.get('paper_width_mm') or 80)
        pixel_width = 384 if paper_width <= 58 else 576

        if ctx:
            tamil_renderer = TamilReceiptRasterRenderer(self.settings)
            image = tamil_renderer.render_receipt_image(ctx, paper_width_mm=paper_width)
        else:
            # Fallback for line-based callers
            margin = 4
            font_path = self._unicode_font_path()
            font_size = 20 if pixel_width <= 384 else 24
            content = [str(line or '') for line in lines]

            renderer = ShapedFontRenderer(font_path, font_size)
            while font_size > 10:
                widest = max((renderer.line_width(line) for line in content), default=0)
                if widest <= pixel_width - (margin * 2):
                    break
                font_size -= 1
                renderer = ShapedFontRenderer(font_path, font_size)

            line_height = max(font_size + 4, int(renderer._ascent_px * 1.3) + 6)
            image_height = max(line_height, line_height * len(content) + (margin * 2))
            image = Image.new('L', (pixel_width, image_height), 255)
            y = margin
            for line in content:
                renderer.draw_line(image, margin, y, line, fill=0)
                y += line_height

        return self.generate_image_raster_bytes(image)

    def generate_receipt_bytes(self, lines, rasterize_unicode=False, ctx=None):
        """Build raw ESC/POS bytes from pre-formatted receipt lines or structured context.

        CRITICAL: For English, this method renders lines exactly as given by receipt_formatter.
        For Tamil, it uses TamilReceiptRasterRenderer with structured ctx for pixel-anchored layout.
        """
        if rasterize_unicode:
            return self._generate_raster_receipt_bytes(lines, ctx=ctx)

        from escpos.printer import Dummy

        # ── DEBUG: log ESC/POS receipt path ───────────────────────────────────
        logger.debug('ESC/POS RECEIPT: chars_per_line=%d  formatter=EscposGenerator  lines=%d', WIDTH, len(lines))
        for ln in lines:
            logger.debug('ESC/POS RECEIPT LINE: %r', ln)

        p = Dummy()
        width = self._line_width()
        # Activate Font A (12×24 dots) FIRST — before any text — so the printer
        # uses 48 columns for the entire receipt on 80mm paper.
        self._inject_font_a(p)
        self._reset_style(p)
        self._set_density(p)
        # ESC/POS darkness: double-strike (ESC E 1) stays ON for entire receipt.
        # Combined with density=9 this produces the darkest possible thermal output.
        # Turned off only at the very end after cut.
        try:
            if hasattr(p, '_raw'):
                p._raw(b'\x1b\x45\x01')  # ESC E 1 — emphasize/double-strike ON
        except Exception:
            pass

        for line in lines:
            # Treat None as blank; preserve empty strings (blank separator lines)
            if line is None:
                continue

            text = str(line)

            # Hard-clip safety net: only for ASCII (English). Never clip Tamil
            # lines here -- Tamil uses rasterize_unicode path, not text mode.
            if len(text) > width and text.isascii():
                text = text[:width]

            stripped = text.strip()

            try:
                if self._is_thin_separator(text):
                    # Separators: bold, render the repeated char at full width
                    p.set(bold=True)
                    p.text(text.rstrip() + '\n')
                    self._reset_style(p)
                    self._set_density(p)
                    continue

                if self._is_bold_separator(text):
                    p.set(bold=True)
                    p.text(text.rstrip() + '\n')
                    self._reset_style(p)
                    self._set_density(p)
                    continue

                if self._is_total_line(text):
                    # Bold only — do NOT use double_height (it shifts alignment on
                    # many thermal printers and the formatter already right-aligns
                    # the amount in fixed-width monospace).
                    p.set(align='left', bold=True)
                    p.text(text.rstrip() + '\n')
                    self._reset_style(p)
                    self._set_density(p)
                    continue

                if self._is_nutrition_title(text):
                    # Formatter already embedded centering spaces — render as-is
                    # with left alignment so the spaces are preserved exactly.
                    p.set(align='left', bold=True)
                    p.text(text.rstrip() + '\n')
                    self._reset_style(p)
                    self._set_density(p)
                    continue

                if self._is_thanks_line(text):
                    # Formatter already embedded centering spaces — render as-is.
                    p.set(align='left', bold=True)
                    p.text(text.rstrip() + '\n')
                    self._reset_style(p)
                    self._set_density(p)
                    continue

                if not stripped:
                    # Blank separator line between wrapped items — emit one newline
                    p.text('\n')
                    continue

                # All other lines: bold, left-aligned, preserve formatter spacing
                p.set(bold=True)
                p.text(text.rstrip() + '\n')

            except Exception as e:
                logger.warning('ESC/POS line encode failed: %s', e)
                try:
                    safe = text.encode('ascii', errors='replace').decode('ascii')
                    p.text(safe.rstrip() + '\n')
                except Exception:
                    pass

        self._reset_style(p)
        # Turn off double-strike after receipt to leave printer in clean state
        self._clear_emphasis(p)

        if self.settings.get('auto_cut', True):
            try:
                p.cut()
            except Exception:
                p.text('\n')

        if hasattr(p, 'output'):
            return p.output
        if hasattr(p, 'get_output'):
            return p.get_output()
        raise RuntimeError('Could not extract ESC/POS output')
