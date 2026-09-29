"""
Proper-shaping text renderer for the thermal raster receipt (Tamil-safe).

WHY THIS FILE EXISTS
─────────────────────
PIL's ImageDraw.text() only reorders Tamil pre-base vowel signs correctly
(e.g. 'திரு' keeping its 'ு', 'வாடிக்கையாளர்' not jumbling into
'வாடிக்கயைாளர்') if Pillow was compiled with libraqm. The official PyPI
Windows wheels for Pillow do NOT bundle raqm (see Pillow/Pillow#7126 on
GitHub), so on a Windows machine PIL silently falls back to "basic" layout
-- it draws each codepoint's glyph in *storage* order with no reordering.
Tamil vowel signs ெ ே ை (and the split signs ொ ோ) are stored AFTER their
consonant in Unicode but must be drawn BEFORE it, so basic layout visibly
scrambles any word containing them. This is exactly the corruption seen in
the /debug/tamil-raster preview ("திருமேனி" -> "திரமேனி",
"வாடிக்கையாளர்" -> "வாடிக்கயைாளர்", "செலுத்தியது" -> "சலெுத்தியது", etc).

This module shapes text with HarfBuzz (uharfbuzz -- ships prebuilt wheels
for Windows/macOS/Linux, no system libraqm install required) and rasterizes
the shaped glyphs with FreeType (freetype-py), pasting glyph bitmaps onto
the PIL canvas by hand. This gives correct Tamil shaping on every machine,
independent of how the local Pillow build was compiled.
"""

import logging
import os
import sys
import unicodedata
import uharfbuzz as hb
import freetype
from PIL import Image, ImageDraw

logger = logging.getLogger('billing.printer')


def bundled_font_dirs():
    """Directories that may hold the bundled Noto Sans Tamil fonts.
    Dev: backend/fonts. PyInstaller (desktop_app.spec datas): _MEIPASS/backend/fonts."""
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [os.path.normpath(os.path.join(here, '..', 'fonts'))]
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        dirs.insert(0, os.path.join(sys._MEIPASS, 'backend', 'fonts'))
    return dirs


def grapheme_clusters(text):
    """Split into user-perceived characters: a base char plus following combining
    marks (Tamil vowel signs and virama are Mn/Mc), ZWJ/ZWNJ kept attached.
    Breaking between clusters never splits a Tamil letter from its signs."""
    clusters = []
    for ch in str(text or ''):
        if clusters and (unicodedata.category(ch) in ('Mn', 'Mc', 'Me') or ch in '\u200c\u200d'
                         or clusters[-1].endswith(('\u200c', '\u200d'))):
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


class ShapedFontRenderer:
    """Renders Unicode text with proper HarfBuzz shaping and FreeType rasterization."""

    def __init__(self, font_path, font_size_px):
        self.font_path = font_path
        self.font_size_px = font_size_px

        with open(font_path, 'rb') as f:
            font_bytes = f.read()

        hb_face = hb.Face(font_bytes)
        self.hb_font = hb.Font(hb_face)
        self._upem = hb_face.upem or 1000
        self.hb_font.scale = (self._upem, self._upem)

        self.ft_face = freetype.Face(font_path)
        self.ft_face.set_pixel_sizes(0, font_size_px)
        self._ascent_px = self.ft_face.size.ascender / 64.0

    def _shape(self, text):
        """Shape text -> list of (glyph_id, x_advance, x_offset, y_offset) in pixels."""
        if not text:
            return []
        buf = hb.Buffer()
        buf.add_str(str(text))
        buf.guess_segment_properties()
        hb.shape(self.hb_font, buf)

        scale = self.font_size_px / self._upem
        return [
            (info.codepoint, pos.x_advance * scale, pos.x_offset * scale, pos.y_offset * scale)
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions)
        ]

    def line_width(self, text):
        if not text:
            return 0
        return sum(g[1] for g in self._shape(text))

    def draw_line(self, canvas_img, x, y, text, fill=0):
        """Draw one shaped line onto a PIL 'L' or 'RGB' image.
        x, y = top-left of the text line's bounding box.
        Returns the pen's final x position.
        """
        if not text:
            return x

        is_rgb = canvas_img.mode == 'RGB'
        fr, fg, fb = (fill, fill, fill) if isinstance(fill, int) else fill
        px = canvas_img.load()
        w_img, h_img = canvas_img.size

        pen_x = float(x)
        baseline_y = y + self._ascent_px

        for glyph_id, x_adv, x_off, y_off in self._shape(text):
            self.ft_face.load_glyph(glyph_id, freetype.FT_LOAD_RENDER)
            glyph = self.ft_face.glyph
            bitmap = glyph.bitmap
            bw, bh = bitmap.width, bitmap.rows

            if bw and bh:
                gx = int(pen_x + x_off + glyph.bitmap_left)
                gy = int(baseline_y - y_off - glyph.bitmap_top)
                buf = bitmap.buffer
                pitch = bitmap.pitch
                for row in range(bh):
                    iy = gy + row
                    if iy < 0 or iy >= h_img:
                        continue
                    row_off = row * pitch
                    for col in range(bw):
                        ix = gx + col
                        if ix < 0 or ix >= w_img:
                            continue
                        alpha = buf[row_off + col]
                        if not alpha:
                            continue
                        t = alpha / 255.0
                        if is_rgb:
                            r0, g0, b0 = px[ix, iy]
                            px[ix, iy] = (
                                int(r0 * (1 - t) + fr * t),
                                int(g0 * (1 - t) + fg * t),
                                int(b0 * (1 - t) + fb * t),
                            )
                        else:
                            existing = px[ix, iy]
                            px[ix, iy] = int(existing * (1 - t) + fill * t)
            pen_x += x_adv

        return pen_x

    def draw_line_right_aligned(self, canvas_img, right_x, y, text, fill=0):
        """Draw text so its rendered right edge lands at right_x."""
        w = self.line_width(text)
        return self.draw_line(canvas_img, right_x - w, y, text, fill=fill)

    def draw_divider(self, canvas_img, x0, x1, y, style='thin', fill=0):
        """Draw true horizontal rule from x0 to x1 pixels."""
        draw = ImageDraw.Draw(canvas_img)
        y = int(round(y))
        if style == 'bold':
            draw.line([(x0, y), (x1, y)], fill=fill, width=2)
            draw.line([(x0, y + 3), (x1, y + 3)], fill=fill, width=2)
        else:
            draw.line([(x0, y), (x1, y)], fill=fill, width=1)


class TamilReceiptRasterRenderer:
    """Structured pixel-based Tamil receipt renderer for 80mm/58mm thermal printers.

    Lays out every section (Header, Metadata, Items, Summary, Total, Payment, Nutrition, Footer)
    using modular vertical regions, dedicated non-overlapping rows, and fixed pixel anchors.
    """

    LABELS = {
        'estimate':    '-- மதிப்பீடு --',
        'phone':       'போன்:',
        'bill_no':     'பில் எண்',
        'date':        'தேதி',
        'time':        'நேரம்',
        'customer':    'வாடிக்கையாளர்',
        'phone_cust':  'தொலைபேசி',
        'no':          'எண்',
        'product':     'பொருள்',
        'qty':         'அளவு',
        'amt':         'தொகை',
        'total':       'மொத்தம்',
        'paid':        'செலுத்தியது',
        'returned':    'மீதம்',
        'due':         'நிலுவை',
        'nutrition':   'ஊட்டச்சத்து',
        'protein':     'புரதம்',
        'abs_protein': 'உறிஞ்சும் புரதம்',
        'fat':         'கொழுப்பு',
        'carbs':       'மாவுச்சத்து',
        'fiber':       'நார்ச்சத்து',
        'moisture':    'ஈரப்பதம்',
        'ash':         'சாம்பல் (Ash)',
        'thanks':      'நன்றி மீண்டும் வருக',
        'items':       'பொருட்கள்',
        'items_qty':   'அளவு',
    }

    def __init__(self, settings=None):
        self.settings = settings or {}

    def _resolve_font_path(self, bold=True):
        """Locate Tamil TrueType font for thermal printing."""
        configured = (self.settings.get('unicode_font_path') or '').strip()
        font_file = 'NotoSansTamil-Bold.ttf' if bold else 'NotoSansTamil-Regular.ttf'
        font_dirs = bundled_font_dirs()
        bundled = os.path.join(font_dirs[-1], font_file)

        candidates = [
            configured if configured and os.path.isfile(configured) else None,
            *[os.path.join(d, font_file) for d in font_dirs],
            *[os.path.join(d, 'NotoSansTamil-Bold.ttf') for d in font_dirs],
            *[os.path.join(d, 'NotoSansTamil-Regular.ttf') for d in font_dirs],
            os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'Nirmala.ttc'),
            os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'Nirmala.ttf'),
            os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts', 'Latha.ttf'),
        ]
        for p in candidates:
            if p and os.path.isfile(p):
                return p

        raise RuntimeError(f'Tamil font not found. Expected bundled font at: {bundled}')

    @staticmethod
    def _item_product_name(item):
        return (
            item.get('tamil_name') or item.get('name_tamil') or
            item.get('name_ta') or item.get('tamil') or item.get('name') or ''
        )

    @staticmethod
    def _format_qty(qty):
        try:
            q = float(qty or 0)
            if abs(q - round(q)) < 0.0001:
                return str(int(round(q)))
            return f'{q:.3f}'.rstrip('0').rstrip('.')
        except Exception:
            return str(qty or '')

    @staticmethod
    def _format_amt(amount):
        try:
            return f'{float(amount or 0):.2f}'
        except Exception:
            return str(amount or '0.00')

    def _money(self, amount):
        try:
            val = float(amount or 0)
            if val < 0:
                return f'-₹{abs(val):.2f}'
            return f'₹{val:.2f}'
        except Exception:
            return f'₹{amount or "0.00"}'

    @staticmethod
    def _format_time(time_str):
        t = (time_str or '').strip()
        if not t:
            return ''
        parts = t.split()
        if parts and ':' in parts[0]:
            hms = parts[0].split(':')
            if len(hms) == 3:
                parts[0] = hms[0] + ':' + hms[1]
        return ' '.join(parts)

    @staticmethod
    def _split_long_word(word, max_w, renderer):
        """Break a single word wider than max_w at grapheme-cluster boundaries
        (AUDIT FIX: unbroken names used to overprint the Qty/Amount columns)."""
        parts, current = [], ''
        for cluster in grapheme_clusters(word):
            candidate = current + cluster
            if current and renderer.line_width(candidate) > max_w:
                parts.append(current)
                current = cluster
            else:
                current = candidate
        if current:
            parts.append(current)
        return parts

    @staticmethod
    def wrap_text_pixel_width(text, max_w, renderer):
        """Split text into lines so each line does not exceed max_w pixels."""
        words = []
        for word in str(text or '').split():
            if renderer.line_width(word) > max_w:
                words.extend(TamilReceiptRasterRenderer._split_long_word(word, max_w, renderer))
            else:
                words.append(word)
        if not words:
            return ['']
        lines = []
        current = ''
        for word in words:
            candidate = (current + ' ' + word).strip() if current else word
            if renderer.line_width(candidate) <= max_w:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines if lines else ['']

    def render_receipt_image(self, ctx, paper_width_mm=None):
        """Render complete structured Tamil receipt onto a high-contrast PIL bitmap."""
        paper_w = int(paper_width_mm or self.settings.get('paper_width_mm') or 80)
        pixel_width = 384 if paper_w <= 58 else 576

        font_path = self._resolve_font_path(bold=True)
        # Font size hierarchy
        body_font_size = 20 if pixel_width <= 384 else 22
        title_font_size = body_font_size + 4

        body_renderer = ShapedFontRenderer(font_path, body_font_size)
        title_renderer = ShapedFontRenderer(font_path, title_font_size)

        margin = 4 if pixel_width <= 384 else 6
        content_x0 = margin
        content_x1 = pixel_width - margin
        content_w = content_x1 - content_x0

        L = self.LABELS
        shop = ctx.get('shop') or {}
        bill = ctx.get('bill') or {}
        nutr = ctx.get('nutrition') or {}
        items = bill.get('items') or []
        is_estimate = ctx.get('is_estimate', False)
        is_normal = ctx.get('is_normal', not is_estimate)
        is_oil = ctx.get('is_oil_product', False)

        line_h = max(body_font_size + 6, int(body_renderer._ascent_px * 1.3) + 6)
        title_line_h = max(title_font_size + 6, int(title_renderer._ascent_px * 1.3) + 6)

        # ── Item column anchors ──────────────────────────────────────────────
        sno_col_w = max(body_renderer.line_width(L['no']), body_renderer.line_width('99')) + 8
        name_x = content_x0 + int(sno_col_w)
        amt_right_x = content_x1

        # Qty right anchor: leave room for Amount column
        amt_gutter = 95 if pixel_width <= 384 else 125
        qty_right_x = amt_right_x - amt_gutter

        # Max width of quantity text (e.g. '999.999' or 'அளவு')
        qty_max_text_w = max(
            body_renderer.line_width(L['qty']),
            body_renderer.line_width('999.999'),
        ) + 6
        qty_left_x = qty_right_x - qty_max_text_w

        # Strict visual safety gap of 18-20px before Qty column
        safety_gap_px = 18 if pixel_width <= 384 else 20
        product_right_limit = qty_left_x - safety_gap_px
        max_name_w = max(60, int(product_right_limit - name_x))

        # ── Build operations ──────────────────────────────────────────────────
        ops = []
        y_cursor = [margin + 2]

        def alloc_row(h=None):
            row_h = h if h is not None else line_h
            y0 = y_cursor[0]
            y_cursor[0] += row_h
            return y0

        def add_center(text, use_title=False):
            if not text:
                return
            r = title_renderer if use_title else body_renderer
            h = title_line_h if use_title else line_h
            # Word wrap centered text if it exceeds content width
            wrapped = self.wrap_text_pixel_width(text, content_w, r)
            for line_txt in wrapped:
                def _op(img, y0, text=line_txt, r=r):
                    w = r.line_width(text)
                    x = content_x0 + max(0, (content_w - w) // 2)
                    r.draw_line(img, x, y0, text, fill=0)
                ops.append((alloc_row(h), _op))

        def add_divider(style='thin'):
            h = 10 if style == 'bold' else 7
            def _op(img, y0, style=style):
                body_renderer.draw_divider(img, content_x0, content_x1, y0 + (h // 2), style=style, fill=0)
            ops.append((alloc_row(h), _op))

        def add_label_value(label, value, rx=None):
            right_edge = rx if rx is not None else content_x1
            def _op(img, y0, label=label, value=value, right_edge=right_edge):
                body_renderer.draw_line(img, content_x0, y0, str(label), fill=0)
                body_renderer.draw_line_right_aligned(img, right_edge, y0, str(value), fill=0)
            ops.append((alloc_row(), _op))

        def add_left_text(text):
            def _op(img, y0, text=text):
                body_renderer.draw_line(img, content_x0, y0, str(text), fill=0)
            ops.append((alloc_row(), _op))

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 1: HEADER
        # ═════════════════════════════════════════════════════════════════════
        if is_estimate:
            add_divider('bold')
            add_center(L['estimate'], use_title=True)

        if is_normal:
            add_divider('bold')
            shop_name = (shop.get('name_ta') or shop.get('name_en') or '').strip()
            if shop_name:
                add_center(shop_name, use_title=True)
            add_divider('bold')

            for k in ('address_line1_ta', 'address_line2_ta', 'address_line3_ta'):
                addr = (shop.get(k) or '').strip()
                if addr:
                    add_center(addr)

            phone = (shop.get('phone') or '').strip()
            if phone:
                add_center(f"{L['phone']} {phone}".strip())

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 2: BILL METADATA (DEDICATED, COLLISION-FREE ROWS)
        # ═════════════════════════════════════════════════════════════════════
        add_divider('thin')

        # 1. Bill Number (Dedicated row)
        add_left_text(f"{L['bill_no']} : {bill.get('id', '')}")

        # 2. Date & Time (Width-aware: single row if safe gap >= 20px, else 2 rows)
        date_val = (bill.get('date') or '').strip()
        time_val = self._format_time(bill.get('time'))
        date_lbl = f"{L['date']} : {date_val}" if date_val else ""
        time_lbl = f"{L['time']} : {time_val}" if time_val else ""

        if date_lbl and time_lbl:
            w_date = body_renderer.line_width(date_lbl)
            w_time = body_renderer.line_width(time_lbl)
            if w_date + w_time + 24 <= content_w:
                # Safely fit on one row without overlap
                def _date_time_op(img, y0, d_txt=date_lbl, t_txt=time_lbl):
                    body_renderer.draw_line(img, content_x0, y0, d_txt, fill=0)
                    body_renderer.draw_line_right_aligned(img, content_x1, y0, t_txt, fill=0)
                ops.append((alloc_row(), _date_time_op))
            else:
                add_left_text(date_lbl)
                add_left_text(time_lbl)
        elif date_lbl:
            add_left_text(date_lbl)
        elif time_lbl:
            add_left_text(time_lbl)

        # 3. Customer Name (Dedicated row, multi-line wrap if long)
        cust_name = (bill.get('customer_name') or '').strip()
        if cust_name:
            prefix = f"{L['customer']} : "
            prefix_w = body_renderer.line_width(prefix)
            name_avail_w = content_w - prefix_w
            wrapped_cust = self.wrap_text_pixel_width(cust_name, name_avail_w, body_renderer)
            # Line 1: prefix + first chunk
            add_left_text(prefix + wrapped_cust[0])
            # Continuation lines indented
            for sub_c in wrapped_cust[1:]:
                def _sub_cust_op(img, y0, text=sub_c, ind=content_x0 + int(prefix_w)):
                    body_renderer.draw_line(img, ind, y0, text, fill=0)
                ops.append((alloc_row(line_h - 2), _sub_cust_op))

        # 4. Customer Phone (Dedicated row)
        cust_phone = (bill.get('customer_phone') or '').strip()
        if cust_phone:
            add_left_text(f"{L['phone_cust']} : {cust_phone}")

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 3: ITEMS TABLE
        # ═════════════════════════════════════════════════════════════════════
        add_divider('thin')

        def _hdr_op(img, y0):
            body_renderer.draw_line(img, content_x0, y0, L['no'], fill=0)
            body_renderer.draw_line(img, name_x, y0, L['product'], fill=0)
            body_renderer.draw_line_right_aligned(img, qty_right_x, y0, L['qty'], fill=0)
            body_renderer.draw_line_right_aligned(img, amt_right_x, y0, L['amt'], fill=0)
        ops.append((alloc_row(), _hdr_op))
        add_divider('thin')

        wrapped_count = 0
        for i, it in enumerate(items, 1):
            name = self._item_product_name(it)
            qty_s = self._format_qty(it.get('qty'))
            amt_s = self._format_amt(it.get('amount'))

            wrapped_lines = self.wrap_text_pixel_width(name, max_name_w, body_renderer)
            if len(wrapped_lines) > 1:
                wrapped_count += 1

            # Line 1: Sno, first line of product, Qty, Amount
            line1_text = wrapped_lines[0]
            def _item_line1_op(img, y0, no=i, name=line1_text, qty=qty_s, amt=amt_s):
                body_renderer.draw_line(img, content_x0, y0, str(no), fill=0)
                body_renderer.draw_line(img, name_x, y0, name, fill=0)
                body_renderer.draw_line_right_aligned(img, qty_right_x, y0, qty, fill=0)
                body_renderer.draw_line_right_aligned(img, amt_right_x, y0, amt, fill=0)
            ops.append((alloc_row(), _item_line1_op))

            # Continuation lines for long product names
            for sub_line in wrapped_lines[1:]:
                def _sub_op(img, y0, text=sub_line):
                    body_renderer.draw_line(img, name_x, y0, text, fill=0)
                ops.append((alloc_row(line_h - 2), _sub_op))

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 4: SUMMARY SECTION
        # ═════════════════════════════════════════════════════════════════════
        add_divider('thin')
        n_items = int(bill.get('total_items') or len(items))
        tot_qty = self._format_qty(bill.get('total_qty'))
        items_txt = f"{L['items']} : {n_items}"
        qty_txt = f"{L['items_qty']} : {tot_qty}"

        def _summary_op(img, y0, it_t=items_txt, q_t=qty_txt):
            body_renderer.draw_line(img, content_x0, y0, it_t, fill=0)
            body_renderer.draw_line_right_aligned(img, content_x1, y0, q_t, fill=0)
        ops.append((alloc_row(), _summary_op))

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 5: TOTAL & DISCOUNTS
        # ═════════════════════════════════════════════════════════════════════
        subtotal = float(bill.get('subtotal', 0) or 0)
        discount = float(bill.get('discount', 0) or 0)
        tot_val = self._money(bill.get('total', 0))

        if discount > 0:
            add_label_value('கூட்டுத்தொகை :', self._money(subtotal))
            add_label_value('தள்ளுபடி :', f"-{self._money(discount)}")

        add_divider('bold')

        def _total_op(img, y0, tot=tot_val):
            title_renderer.draw_line(img, content_x0, y0, L['total'], fill=0)
            title_renderer.draw_line_right_aligned(img, amt_right_x, y0, tot, fill=0)
        ops.append((alloc_row(title_line_h), _total_op))
        add_divider('bold')

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 6: PAYMENT & DUE
        # ═════════════════════════════════════════════════════════════════════
        pm = str(bill.get('payment_method') or bill.get('payment_mode') or '').strip()
        if pm:
            pm_ta = {'cash': 'ரொக்கம் (Cash)', 'upi': 'யுபிஐ (UPI)', 'credit': 'கடன் (Credit)'}.get(pm.lower(), pm)
            add_label_value('செலுத்தும் முறை :', pm_ta)

        add_label_value(f"{L['paid']} :", self._money(bill.get('paid', 0)))
        returned = float(bill.get('returned', 0) or 0)
        if returned > 0:
            add_label_value(f"{L['returned']} :", self._money(returned))
        due = float(bill.get('due', 0) or bill.get('due_amount', 0) or 0)
        if due > 0:
            add_label_value(f"{L['due']} :", self._money(due))

        loyalty = bill.get('loyalty') or {}
        if isinstance(loyalty, dict) and loyalty.get('stars') is not None:
            add_label_value('வெகுமதி புள்ளிகள் :', f"★ {loyalty.get('stars')}")

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 7: NUTRITION
        # ═════════════════════════════════════════════════════════════════════
        nutr_rows = []
        for k, lbl in (
            ('protein', L['protein']),
            ('absorbable_protein', L['abs_protein']),
            ('fat', L['fat']),
            ('carbs', L['carbs']),
            ('fiber', L['fiber']),
        ):
            val = float(nutr.get(k, 0) or 0)
            if k == 'absorbable_protein' and val <= 0:
                continue
            nutr_rows.append((lbl, val))

        if is_oil:
            for k, lbl in (('moisture', L['moisture']), ('ash', L['ash'])):
                val = float(nutr.get(k, 0) or 0)
                if val > 0:
                    nutr_rows.append((lbl, val))
        else:
            ash_val = float(nutr.get('ash', 0) or 0)
            if ash_val > 0:
                nutr_rows.append((L['ash'], ash_val))

        if nutr_rows:
            add_divider('thin')
            add_center(f"------ {L['nutrition']} ------")
            add_divider('thin')
            for lbl, val in nutr_rows:
                add_label_value(lbl, f'{val:.1f}%')
            add_divider('thin')

        # ═════════════════════════════════════════════════════════════════════
        # SECTION 8: FOOTER
        # ═════════════════════════════════════════════════════════════════════
        add_center(f"******** {L['thanks']} ********")
        add_divider('bold')

        # ── 10. Generate Final Canvas ─────────────────────────────────────────
        bottom_padding = 12
        total_height = y_cursor[0] + bottom_padding
        image = Image.new('L', (pixel_width, total_height), 255)

        for y0, op in ops:
            op(image, y0)

        logger.info(
            '[TAMIL-RASTER] paper=%dmm canvas=%dx%d font=%s font_size=%dpt items=%d (wrapped=%d)',
            paper_w, pixel_width, total_height, os.path.basename(font_path), body_font_size,
            len(items), wrapped_count
        )

        return image

    def render_raw_raster_test_image(self, paper_width_mm=None):
        """Render small hardware validation raster test bitmap.

        Content:
          - Header: தமிழ் PRINTER TEST
          - Sample Tamil words: வணக்கம், கேழ்வரகு, வேர்க்கடலை, முந்திரி, பாதாம், குதிரைவாலி
          - Mixed Tamil/English: கேழ்வரகு - Ragi, வேர்க்கடலை - Groundnut
          - Currency amounts: ₹1234.50, ₹99.00, ₹175.00
        """
        paper_w = int(paper_width_mm or self.settings.get('paper_width_mm') or 80)
        pixel_width = 384 if paper_w <= 58 else 576

        font_path = self._resolve_font_path(bold=True)
        body_font_size = 20 if pixel_width <= 384 else 22
        title_font_size = body_font_size + 4

        body_renderer = ShapedFontRenderer(font_path, body_font_size)
        title_renderer = ShapedFontRenderer(font_path, title_font_size)

        margin = 4 if pixel_width <= 384 else 6
        content_x0 = margin
        content_x1 = pixel_width - margin
        content_w = content_x1 - content_x0

        line_h = max(body_font_size + 6, int(body_renderer._ascent_px * 1.3) + 6)
        title_line_h = max(title_font_size + 6, int(title_renderer._ascent_px * 1.3) + 6)

        ops = []
        y_cursor = [margin + 2]

        def alloc_row(h=None):
            row_h = h if h is not None else line_h
            y0 = y_cursor[0]
            y_cursor[0] += row_h
            return y0

        def add_center(text, use_title=False):
            if not text:
                return
            r = title_renderer if use_title else body_renderer
            h = title_line_h if use_title else line_h
            wrapped = self.wrap_text_pixel_width(text, content_w, r)
            for line_txt in wrapped:
                def _op(img, y0, text=line_txt, r=r):
                    w = r.line_width(text)
                    x = content_x0 + max(0, (content_w - w) // 2)
                    r.draw_line(img, x, y0, text, fill=0)
                ops.append((alloc_row(h), _op))

        def add_divider(style='thin'):
            h = 10 if style == 'bold' else 7
            def _op(img, y0, style=style):
                body_renderer.draw_divider(img, content_x0, content_x1, y0 + (h // 2), style=style, fill=0)
            ops.append((alloc_row(h), _op))

        def add_left_text(text):
            def _op(img, y0, text=text):
                body_renderer.draw_line(img, content_x0, y0, str(text), fill=0)
            ops.append((alloc_row(), _op))

        def add_label_value(label, value):
            def _op(img, y0, label=label, value=value):
                body_renderer.draw_line(img, content_x0, y0, str(label), fill=0)
                body_renderer.draw_line_right_aligned(img, content_x1, y0, str(value), fill=0)
            ops.append((alloc_row(), _op))

        # Title
        add_divider('bold')
        add_center('தமிழ் PRINTER TEST', use_title=True)
        add_divider('bold')

        # Tamil words
        for w in ['வணக்கம்', 'கேழ்வரகு', 'வேர்க்கடலை', 'முந்திரி', 'பாதாம்', 'குதிரைவாலி']:
            add_left_text(w)

        add_divider('thin')

        # Mixed Tamil/English
        add_left_text('கேழ்வரகு - Ragi')
        add_left_text('வேர்க்கடலை - Groundnut')

        add_divider('thin')

        # Currency test
        add_label_value('பொருள் 1 :', '₹1234.50')
        add_label_value('பொருள் 2 :', '₹99.00')
        add_label_value('பொருள் 3 :', '₹175.00')

        add_divider('bold')
        add_center('******** நன்றி ********')
        add_divider('bold')

        bottom_padding = 12
        total_height = y_cursor[0] + bottom_padding
        image = Image.new('L', (pixel_width, total_height), 255)

        for y0, op in ops:
            op(image, y0)

        logger.info(
            '[TAMIL-RAW-TEST] paper=%dmm canvas=%dx%d font=%s',
            paper_w, pixel_width, total_height, os.path.basename(font_path)
        )
        return image
