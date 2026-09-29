"""
Monospace receipt formatter for 80mm thermal printers — Font B, 48 columns.
Strict fixed-width POS grid — no tabs, flex, or responsive layout.

Unicode-safe Tamil rendering using wcwidth.wcswidth() for all width
calculations.  NEVER uses len() for alignment — all padding uses
_pad_right / _pad_left / _center which are wcswidth-aware.

FONT MODE: Font B (9×17 dots) on 80mm paper → 48 usable columns.
  576 printer dots / 12 dot-width = 48 stable columns.
  (80mm paper @ 203dpi ≈ 576 printable dots)

Column layout (48 chars total):

  English receipt width: 48 chars
    sno(3) + sep(1) + name(27) + qty(5) + sep(1) + amt(11) = 48
      sno   : right-padded  e.g. "1  "
      name  : left-aligned, word-wrapped  e.g. "RED FOXTAIL MILLET         "
      qty   : right-aligned e.g. "    1"
      amt   : right-aligned e.g. "      90.00"

  Tamil receipt width: 48 chars
    sno(3) + name(24) + qty(8) + amt(13) = 48
      name=24: wcswidth-measured; typical Tamil product names fit well
      qty=8:   right-aligned numeric, stable regardless of Tamil glyph width
      amt=13:  'Rs.99999.00' (11 chars) + 2-char buffer for alignment safety

  Nutrition row (48 chars):
    label(37) + val(11) = 48

ESC/POS safety: All output is ASCII-only.
  - Rs.  used instead of ₹ (U+20B9) — not in PC437 code page
  - *    used instead of ♥ (U+2665) — not in PC437 code page
"""

try:
    from wcwidth import wcswidth as _wcswidth
    import unicodedata

    def _vlen(text: str) -> int:
        """Visual width of text, correctly handling Tamil combining characters."""
        text = str(text or '')
        length = 0
        for char in text:
            # If character is a combining mark (Mn, Mc, Me), it has 0 visual width in monospace
            if unicodedata.category(char) in ('Mn', 'Mc', 'Me'):
                continue
            
            # Use wcswidth for base character to handle fullwidth CJK etc.
            # fallback to 1 if wcswidth is -1 (e.g. for some control chars, though they shouldn't be here)
            w = _wcswidth(char)
            length += w if w >= 0 else 1
            
        return length

except ImportError as e:
    raise ImportError("wcwidth package is strictly required for proper Unicode display-width calculations (Tamil alignment). Run: pip install wcwidth") from e


# ── Unicode-safe padding helpers ──────────────────────────────────────────────

def _pad_right(text: str, width: int) -> str:
    """Left-align text in a field of `width` visual columns."""
    text = str(text or '')
    padding = max(0, width - _vlen(text))
    return text + (' ' * padding)


def _pad_left(text: str, width: int) -> str:
    """Right-align text in a field of `width` visual columns."""
    text = str(text or '')
    padding = max(0, width - _vlen(text))
    return (' ' * padding) + text


def _center(text: str, width: int) -> str:
    """Center text in a field of `width` visual columns."""
    text = str(text or '').strip()
    if not text:
        return ' ' * width
    total = max(0, width - _vlen(text))
    left = total // 2
    right = total - left
    return (' ' * left) + text + (' ' * right)


def _wrap_words(text: str, width: int) -> list:
    """
    Word-wrap text into lines of at most `width` visual columns.
    NEVER splits inside a Tamil word/grapheme — always breaks on whitespace.
    Words wider than `width` are left unbroken (printer will handle overflow).
    """
    words = str(text or '').split()
    if not words:
        return ['']
    lines = []
    current = ''
    for word in words:
        candidate = (current + ' ' + word).strip() if current else word
        if _vlen(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines if lines else ['']


class ReceiptFormatter:
    """Build plain-text receipt lines from print context dict."""

    # ── Width profiles ────────────────────────────────────────────────────────
    # 80mm paper, Font B (9×17 dots): 576 dots / 12 = 48 columns.
    #
    # English: 48 cols
    #   sno(3) + sep(1) + name(27) + qty(5) + sep(1) + amt(11) = 48
    #   amt=11 fits 'Rs.99999.00' (11 chars) without truncation
    #
    # Tamil: 48 char-positions
    #   sno(3) + name(27) + qty(7) + amt(11) = 48
    TAMIL_WIDTH   = 48
    ENGLISH_WIDTH = 48

    # Tamil column widths (sum = 48):
    #   sno(3) + name(24) + qty(8) + amt(13) = 48
    #   amt=13 fits 'Rs.99999.00' (11 chars) with 2-char right buffer
    #   qty=8  gives a stable right-aligned numeric field for Tamil glyphs
    #   name=24 accommodates wcswidth of typical Tamil product names
    TA_COL_SNO   = 3
    TA_COL_NAME  = 24
    TA_COL_QTY   = 8
    TA_COL_AMT   = 13   # 3+24+8+13 = 48 ✓

    # English column widths (sum = 48):
    # sno(3) + sep(1) + name(27) + qty(5) + sep(1) + amt(11) = 48
    EN_COL_SNO   = 3
    EN_COL_SEP   = 1    # space between sno and name
    EN_COL_NAME  = 27   # wide product name — uses most of available width
    EN_COL_QTY   = 5    # 5 chars: right-aligned, e.g. "    1"
    EN_COL_SEP2  = 1    # space between qty and amt
    EN_COL_AMT   = 11   # fits 'Rs.99999.00'; total rows use _money()

    # Verify column sums (used in assertions / tests):
    #   EN: 3+1+27+5+1+11 = 48  ✓
    #   TA: 3+24+8+13     = 48  ✓

    # Metadata label widths
    TA_META_LABEL_W    = 14
    EN_META_LABEL_W    = 12
    TA_SUMMARY_LABEL_W = 14
    EN_SUMMARY_LABEL_W = 12
    TA_NUTR_LABEL_W    = 35  # 35+13 = 48 = TAMIL_WIDTH  (matches new TA_COL_AMT=13)
    EN_NUTR_LABEL_W    = 37  # 37+11 = 48 = ENGLISH_WIDTH
    TA_NUTR_VAL_W      = 13  # aligns val column with receipt amount column
    EN_NUTR_VAL_W      = 11

    LABELS = {
        'english': {
            'estimate':   '-- ESTIMATE --',
            'phone':      'Ph:',
            'bill_no':    'Bill No',
            'date':       'Date',
            'time':       'Time',
            'customer':   'Customer',
            'phone_cust': 'Phone',
            'no':         'No',
            'product':    'Product',
            'qty':        'Qty',
            'amt':        'Amt',
            'total':      'TOTAL',
            'paid':       'Paid',
            'returned':   'Returned',
            'due':        'Due',
            'subtotal':   'Subtotal',
            'discount':   'Discount',
            'nutrition':  'NUTRITION',
            'protein':    'Protein',
            'abs_protein':'Abs Protein',
            'fat':        'Fat',
            'carbs':      'Carbs',
            'fiber':      'Fiber',
            'moisture':   'Moisture',
            'ash':        'Ash',
            'thanks':     'THANK YOU VISIT AGAIN',
            'items':      'Items',
            'items_qty':  'Qty',
        },
        'tamil': {
            'estimate':   '-- மதிப்பீடு --',
            'phone':      'போன்:',
            'bill_no':    'பில் எண்',
            'date':       'தேதி',
            'time':       'நேரம்',
            'customer':   'வாடிக்கையாளர்',
            'phone_cust': 'தொலைபேசி',
            'no':         'எண்',
            'product':    'பொருள்',
            'qty':        'அளவு',
            'amt':        'தொகை',
            'total':      'மொத்தம்',
            'paid':       'செலுத்தியது',
            'returned':   'மீதம்',
            'due':        'நிலுவை',
            'subtotal':   'கூட்டுத்தொகை',
            'discount':   'தள்ளுபடி',
            'nutrition':  'ஊட்டச்சத்து',
            'protein':    'புரதம்',
            'abs_protein':'உறிஞ்சும் புரதம்',
            'fat':        'கொழுப்பு',
            'carbs':      'மாவுச்சத்து',
            'fiber':      'நார்ச்சத்து',
            'moisture':   'ஈரப்பதம்',
            'ash':        'சாம்பல் (Ash)',
            'thanks':     'THANK YOU VISIT AGAIN',
            'items':      'பொருட்கள்',
            'items_qty':  'அளவு',
        },
    }

    def __init__(self, chars_per_line=None):
        # chars_per_line accepted for backward compat but ignored;
        # width is chosen per-language at render time.
        pass

    # ── Width/sep helpers ─────────────────────────────────────────────────────

    def _width(self, tamil: bool) -> int:
        return self.TAMIL_WIDTH if tamil else self.ENGLISH_WIDTH

    def thin_sep(self, tamil: bool = False) -> str:
        return '-' * self._width(tamil)

    def bold_sep(self, tamil: bool = False) -> str:
        return '=' * self._width(tamil)

    # Backward-compat aliases (no-arg versions, default English)
    def sep(self):
        return self.thin_sep(False)

    # ── Centering ─────────────────────────────────────────────────────────────

    def center(self, text: str, tamil: bool = False) -> str:
        return _center(text, self._width(tamil))

    def _center(self, text: str, tamil: bool = False) -> str:
        return self.center(text, tamil)

    # ── Key-value metadata line ───────────────────────────────────────────────

    def _label_value_line(self, label: str, value, label_w: int = None,
                          tamil: bool = False) -> str:
        """Fixed-width key-value: 'Bill No     : 108'."""
        default_w = self.TA_META_LABEL_W if tamil else self.EN_META_LABEL_W
        w = label_w or default_w
        w = max(w, _vlen(label))
        lbl = _pad_right(label or '', w)
        return f'{lbl}: {str(value or "").strip()}'

    # ── Address helpers ───────────────────────────────────────────────────────

    def _wrap_address(self, text: str, tamil: bool = False) -> list:
        text = ' '.join((text or '').split())
        if not text:
            return []
        width = self._width(tamil)
        if _vlen(text) <= width:
            return [text]
        return _wrap_words(text, width)

    def _address_lines(self, shop: dict, tamil: bool) -> list:
        if tamil:
            keys = ('address_line1_ta', 'address_line2_ta', 'address_line3_ta')
        else:
            keys = ('address_line1', 'address_line2', 'address_line3')
        lines = []
        for key in keys:
            val = (shop.get(key) or '').strip()
            if val:
                lines.extend(self._wrap_address(val, tamil))
        if not lines:
            fallback = (
                ('address_line1_ta', 'address_line2_ta') if tamil
                else ('address_line1', 'address_line2')
            )
            for key in fallback:
                val = (shop.get(key) or '').strip()
                if val:
                    lines.extend(self._wrap_address(val, tamil))
        return lines

    # ── Scalar formatters ─────────────────────────────────────────────────────

    def _format_qty(self, qty) -> str:
        q = float(qty or 0)
        if abs(q - round(q)) < 0.0001:
            return str(int(round(q)))
        return f'{q:.3f}'.rstrip('0').rstrip('.')

    def _format_amt_money(self, amount) -> str:
        return f'{float(amount or 0):.2f}'

    def _money(self, amount) -> str:
        """ESC/POS-safe: 'Rs.' instead of '₹' (PC437 has no rupee symbol)."""
        return f'Rs.{self._format_amt_money(amount)}'

    def _format_time_display(self, time_str: str) -> str:
        t = (time_str or '').strip()
        if not t:
            return ''
        parts = t.split()
        if parts and ':' in parts[0]:
            hms = parts[0].split(':')
            if len(hms) == 3:
                parts[0] = hms[0] + ':' + hms[1]
        return ' '.join(parts)

    # ── Product name wrapping ─────────────────────────────────────────────────

    def wrap_product_name(self, name: str, width: int = None) -> list:
        """
        Word-wrap product name — NEVER splits Tamil grapheme clusters.
        Uses whitespace-only breaks (text.split()).
        """
        width = width or self.EN_COL_NAME
        return _wrap_words(name, width)

    # ── Tamil item row ────────────────────────────────────────────────────────

    def _format_tamil_item_row(self, no, name: str, qty, amt) -> list:
        """
        Tamil grid: sno(3) + name(24) + qty_r(8) + amt_r(13) = 48 chars

        Single-line:
            '1  செந்தினை                      1    Rs.55.00'

        Multi-line (name wraps):
            '1  முதல் வரி'
            '   கடைசி வரி                      1  Rs.90.00'
        """
        sno_s = str(no)
        qty_s = self._format_qty(qty)
        amt_s = self._format_amt_money(amt)
        indent = ' ' * self.TA_COL_SNO

        # ── Debug: log len() vs wcswidth() discrepancy for Tamil product names ──
        import logging as _logging
        _log = _logging.getLogger('receipt_formatter.debug')
        raw_name = str(name or '')
        _log.debug(
            '[TAMIL WIDTH AUDIT] product=%r  len()=%d  wcswidth()=%d  delta=%d',
            raw_name, len(raw_name), _vlen(raw_name), len(raw_name) - _vlen(raw_name)
        )

        wrapped = _wrap_words(name, self.TA_COL_NAME)
        lines = []

        if len(wrapped) == 1:
            line = (
                _pad_right(sno_s, self.TA_COL_SNO)
                + _pad_right(wrapped[0], self.TA_COL_NAME)
                + _pad_left(qty_s, self.TA_COL_QTY)
                + _pad_left(amt_s, self.TA_COL_AMT)
            )
            lines.append(line)
        else:
            for i, chunk in enumerate(wrapped):
                is_last = (i == len(wrapped) - 1)
                if i == 0:
                    lines.append(_pad_right(sno_s, self.TA_COL_SNO) + chunk)
                elif not is_last:
                    lines.append(indent + chunk)
                else:
                    line = (
                        indent
                        + _pad_right(chunk, self.TA_COL_NAME)
                        + _pad_left(qty_s, self.TA_COL_QTY)
                        + _pad_left(amt_s, self.TA_COL_AMT)
                    )
                    lines.append(line)

        return lines

    # ── English item row ──────────────────────────────────────────────────────

    def _format_english_item_row(self, no, name: str, qty, amt) -> list:
        """
        English grid: sno(3) + sp(1) + name(27) + qty_r(5) + sp(1) + amt_r(11) = 48
        """
        sno_s = str(no)
        qty_s = self._format_qty(qty)
        amt_s = self._format_amt_money(amt)
        indent = ' ' * (self.EN_COL_SNO + self.EN_COL_SEP)  # 4 spaces

        wrapped = _wrap_words(name, self.EN_COL_NAME)
        lines = []

        if len(wrapped) == 1:
            line = (
                _pad_right(sno_s, self.EN_COL_SNO)
                + ' '
                + _pad_right(wrapped[0], self.EN_COL_NAME)
                + _pad_left(qty_s, self.EN_COL_QTY)
                + ' '
                + _pad_left(amt_s, self.EN_COL_AMT)
            )
            lines.append(line)
        else:
            for i, chunk in enumerate(wrapped):
                is_last = (i == len(wrapped) - 1)
                if i == 0:
                    lines.append(_pad_right(sno_s, self.EN_COL_SNO) + ' ' + chunk)
                elif not is_last:
                    lines.append(indent + chunk)
                else:
                    line = (
                        indent
                        + _pad_right(chunk, self.EN_COL_NAME)
                        + _pad_left(qty_s, self.EN_COL_QTY)
                        + ' '
                        + _pad_left(amt_s, self.EN_COL_AMT)
                    )
                    lines.append(line)

        return lines

    # ── Public item row dispatcher ────────────────────────────────────────────

    def format_item_row(self, no, name: str, qty, amt, tamil: bool = False) -> list:
        if tamil:
            return self._format_tamil_item_row(no, name, qty, amt)
        return self._format_english_item_row(no, name, qty, amt)

    # ── Column header ─────────────────────────────────────────────────────────

    def format_product_header(self, L: dict, tamil: bool = False) -> str:
        """
        English: 'No  Product                     Qty         Amt'
                  ^^^  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^ ^^^  ^^^^^^^^^^^
                   3    1+27                         5    1+11
        """
        sno_lbl  = L.get('no')      or ('எண்'   if tamil else 'No')
        name_lbl = L.get('product') or ('பொருள்' if tamil else 'Product')
        qty_lbl  = L.get('qty')     or ('அளவு'  if tamil else 'Qty')
        amt_lbl  = L.get('amt')     or ('தொகை'  if tamil else 'Amt')

        if tamil:
            import logging as _logging
            _log = _logging.getLogger('receipt_formatter.debug')
            for _col, _lbl in [('sno', sno_lbl), ('product', name_lbl), ('qty', qty_lbl), ('amt', amt_lbl)]:
                _log.debug(
                    '[TAMIL HDR AUDIT] col=%s  label=%r  len()=%d  wcswidth()=%d',
                    _col, _lbl, len(_lbl), _vlen(_lbl)
                )
            return (
                _pad_right(sno_lbl, self.TA_COL_SNO)
                + _pad_right(name_lbl, self.TA_COL_NAME)
                + _pad_left(qty_lbl, self.TA_COL_QTY)
                + _pad_left(amt_lbl, self.TA_COL_AMT)
            )
        else:
            # Keep qty column at fixed EN_COL_QTY width; name fills the rest.
            return (
                _pad_right(sno_lbl, self.EN_COL_SNO)
                + ' '
                + _pad_right(name_lbl, self.EN_COL_NAME)
                + _pad_left(qty_lbl, self.EN_COL_QTY)
                + ' '
                + _pad_left(amt_lbl, self.EN_COL_AMT)
            )

    def format_product_row(self, sno, name: str, qty, amount,
                           tamil: bool = False) -> list:
        """Public API — returns list of lines for one product item."""
        name = (name or '').strip()
        if not tamil:
            name = name.upper()
        rows = self.format_item_row(sno, name, qty, amount, tamil=tamil)
        if len(rows) > 1:
            rows.append('')
        return rows

    # ── Total / payment / nutrition ───────────────────────────────────────────

    def format_total_row(self, L: dict, total, tamil: bool = False) -> str:
        """'TOTAL                               Rs.90.00' — label left, amount right."""
        label = (L.get('total') or 'TOTAL').strip()
        amt_s = self._money(total)
        width = self._width(tamil)
        gap = width - _vlen(label) - _vlen(amt_s)
        if gap < 1:
            gap = 1
        return label + (' ' * gap) + amt_s

    def format_payment_line(self, label: str, amount,
                            tamil: bool = False) -> str:
        w = self.TA_META_LABEL_W if tamil else self.EN_META_LABEL_W
        return self._label_value_line(label, self._money(amount), w, tamil)

    def format_nutrition_row(self, label: str, value_pct,
                             tamil: bool = False) -> str:
        """
        Tamil:   'புரதம்                                12.3%'
        English: 'Protein                               12.3%'
        Total width = 48 chars (label=37 + val=11).
        """
        val = f'{float(value_pct or 0):.1f}%'
        lbl = (label or '').strip()
        if tamil:
            return _pad_right(lbl, self.TA_NUTR_LABEL_W) + _pad_left(val, self.TA_NUTR_VAL_W)
        return _pad_right(lbl, self.EN_NUTR_LABEL_W) + _pad_left(val, self.EN_NUTR_VAL_W)

    def format_items_summary_lines(self, L: dict, bill: dict,
                                   tamil: bool = False) -> list:
        n   = int(bill.get('total_items') or len(bill.get('items') or []))
        qty = self._format_qty(bill.get('total_qty'))
        items_str = f"{L['items']} : {n}"
        qty_str = f"{L['items_qty']}   : {qty}"
        width = self._width(tamil)
        spaces = width - _vlen(items_str) - _vlen(qty_str)
        if spaces < 1:
            spaces = 1
        return [items_str + (' ' * spaces) + qty_str]

    def _nutrition_title(self, L: dict, tamil: bool = False) -> str:
        title = (L.get('nutrition') or 'NUTRITION').strip()
        title_with_dashes = f'------ {title} ------'
        return _center(title_with_dashes, self._width(tamil))

    def _footer_thanks(self, L: dict, tamil: bool = False) -> str:
        """ESC/POS-safe footer: '*' instead of '♥' (PC437 has no heart glyph)."""
        thanks = L.get('thanks') or 'THANK YOU VISIT AGAIN'
        stars = '*' * 8
        return _center(f'{stars} {thanks} {stars}', self._width(tamil))

    # ── Language helpers ──────────────────────────────────────────────────────

    def _lang_pack(self, ctx: dict):
        lang = (ctx.get('language') or 'english').lower()
        if lang not in ('tamil', 'english'):
            lang = 'english' if lang.startswith('en') else 'tamil'
        L = self.LABELS.get(lang, self.LABELS['english'])
        return lang, L, lang == 'tamil'

    def _emit(self, lines: list, sections: dict, key: str, line):
        if line is None:
            return
        lines.append(line)
        if key:
            sections[key].append(line)

    def _item_product_name(self, item: dict, tamil: bool) -> str:
        if tamil:
            return (
                item.get('tamil_name') or item.get('name_tamil') or
                item.get('name_ta') or item.get('tamil') or item.get('name') or ''
            )
        return item.get('name') or item.get('name_english') or ''

    # ── Master render ─────────────────────────────────────────────────────────

    def _render(self, ctx: dict):
        lang, L, tamil = self._lang_pack(ctx)
        shop        = ctx.get('shop')      or {}
        bill        = ctx.get('bill')      or {}
        nutr        = ctx.get('nutrition') or {}
        is_estimate = ctx.get('is_estimate', False)
        is_normal   = ctx.get('is_normal',   True)
        is_oil      = ctx.get('is_oil_product', False)

        lines    = []
        sections = {
            'header': [], 'meta': [], 'items': [],
            'total':  [], 'paid': [], 'nutrition': [], 'footer': [],
        }
        e = lambda key, line: self._emit(lines, sections, key, line)

        # ── Header ────────────────────────────────────────────────────────────
        if is_estimate:
            e('header', self.center(L['estimate'], tamil))

        if is_normal:
            e('header', self.bold_sep(tamil))
            shop_name = shop.get('name_ta') if tamil else shop.get('name_en')
            e('header', self.center(shop_name or '', tamil))
            e('header', self.bold_sep(tamil))

            for ln in self._address_lines(shop, tamil):
                e('header', self.center(ln, tamil))

            phone = (shop.get('phone') or '').strip()
            if phone:
                e('header', self.center(f"{L['phone']} {phone}".strip(), tamil))

        # ── Bill metadata ──────────────────────────────────────────────────────
        e('meta', self.thin_sep(tamil))
        w_left = 10 if tamil else 13
        w_right = 9
        pad_width = 25 if tamil else 27
        
        e('meta', self._label_value_line(L['bill_no'], bill.get('id', ''), w_left, tamil))
        
        date_val = (bill.get('date') or '').strip()
        date_str = self._label_value_line(L['date'], date_val, w_left, tamil)
        time_val = self._format_time_display(bill.get('time'))
        if time_val:
            time_str = self._label_value_line(L['time'], time_val, w_right, tamil)
            e('meta', _pad_right(date_str, pad_width) + time_str)
        else:
            e('meta', date_str)
            
        cust_name = bill.get('customer_name')
        cust_phone = bill.get('customer_phone')
        if cust_name or cust_phone:
            cust_str = self._label_value_line(L['customer'], cust_name or '', w_left, tamil) if cust_name else _pad_right('', pad_width)
            phone_str = self._label_value_line(L['phone_cust'], cust_phone, w_right, tamil) if cust_phone else ''
            
            if cust_name and cust_phone:
                e('meta', _pad_right(cust_str, pad_width) + phone_str)
            elif cust_name:
                e('meta', cust_str)
            elif cust_phone:
                e('meta', _pad_right('', pad_width) + phone_str)
        e('meta', self.thin_sep(tamil))

        # ── Items table ────────────────────────────────────────────────────────
        e('items', self.format_product_header(L, tamil))
        e('items', self.thin_sep(tamil))
        for i, it in enumerate(bill.get('items') or [], 1):
            name = self._item_product_name(it, tamil)
            for row in self.format_product_row(
                i, name, it.get('qty'), it.get('amount'), tamil=tamil
            ):
                e('items', row)

        # ── Summary ───────────────────────────────────────────────────────────
        e('items', self.thin_sep(tamil))
        for row in self.format_items_summary_lines(L, bill, tamil):
            e('items', row)

        # ── Discount (only printed when a discount exists — keeps every
        #    no-discount receipt byte-identical to the pre-audit layout) ───────
        discount = float(bill.get('discount', 0) or 0)
        if discount >= 0.01:
            e('total', self.format_payment_line(L.get('subtotal', 'Subtotal'), bill.get('subtotal', 0), tamil))
            e('total', self.format_payment_line(L.get('discount', 'Discount'), -discount, tamil))

        # ── Total ─────────────────────────────────────────────────────────────
        e('total', self.bold_sep(tamil))
        e('total', self.format_total_row(L, bill.get('total', 0), tamil))
        e('total', self.bold_sep(tamil))

        # ── Payment ───────────────────────────────────────────────────────────
        e('paid', self.format_payment_line(L['paid'], bill.get('paid', 0), tamil))
        returned = float(bill.get('returned', 0) or 0)
        if returned > 0:
            e('paid', self.format_payment_line(L['returned'], returned, tamil))
        due = float(bill.get('due', 0) or 0)
        if due >= 0.01:
            e('paid', self.format_payment_line(L.get('due', 'Due'), due, tamil))

        # ── Nutrition ─────────────────────────────────────────────────────────
        e('nutrition', self._nutrition_title(L, tamil))
        for key, label in (
            ('protein',            L['protein']),
            ('absorbable_protein', L['abs_protein']),
            ('fat',                L['fat']),
            ('carbs',              L['carbs']),
            ('fiber',              L['fiber']),
        ):
            val = nutr.get(key, 0) or 0
            if key == 'absorbable_protein' and float(val) <= 0:
                continue
            e('nutrition', self.format_nutrition_row(label, val, tamil))
        if is_oil:
            for key, label in (('moisture', L['moisture']), ('ash', L['ash'])):
                val = nutr.get(key, 0) or 0
                if float(val) > 0:
                    e('nutrition', self.format_nutrition_row(label, val, tamil))
        else:
            ash_val = float(nutr.get('ash', 0) or 0)
            if ash_val > 0:
                e('nutrition', self.format_nutrition_row(L['ash'], ash_val, tamil))
        e('nutrition', self.thin_sep(tamil))

        # ── Footer ────────────────────────────────────────────────────────────
        e('footer', self._footer_thanks(L, tamil))
        e('footer', self.bold_sep(tamil))

        return lines, sections

    # ── Public API ────────────────────────────────────────────────────────────

    def format_receipt(self, ctx: dict) -> list:
        lines, _ = self._render(ctx)
        return lines

    def format_sections(self, ctx: dict) -> dict:
        lines, s = self._render(ctx)
        total_line = s['total'][1] if len(s['total']) > 1 else ''
        return {
            'sep':              self.thin_sep(),
            'bold_sep':         self.bold_sep(),
            'header_lines':     s['header'],
            'meta_lines':       s['meta'],
            'items_lines':      s['items'],
            'items_text':       '\n'.join(s['items']),
            'total_line':       total_line,
            'total_lines':      s['total'],
            'total_text':       '\n'.join(s['total']),
            'paid_lines':       s['paid'],
            'paid_text':        '\n'.join(s['paid']),
            'nutrition_lines':  s['nutrition'],
            'nutrition_text':   '\n'.join(s['nutrition']),
            'footer_lines':     s['footer'],
            'full_text':        '\n'.join(lines),
        }

    def format_preview_text(self, ctx: dict) -> str:
        return '\n'.join(self.format_receipt(ctx))

    # ── Tamil HTML receipt (table/flexbox — no fixed-width text) ─────────────

    def format_tamil_html(self, ctx: dict) -> str:
        """
        Generate a fully HTML-structured Tamil receipt.
        Uses <table> for items, flexbox for totals/metadata, <hr> for separators.
        NO fixed-width text or space padding — works correctly with any Tamil text length.
        """
        lang, L, tamil = self._lang_pack(ctx)
        shop        = ctx.get('shop')      or {}
        bill        = ctx.get('bill')      or {}
        nutr        = ctx.get('nutrition') or {}
        is_estimate = ctx.get('is_estimate', False)
        is_normal   = ctx.get('is_normal',   True)
        is_oil      = ctx.get('is_oil_product', False)

        def esc(s):
            import html as _html
            return _html.escape(str(s or ''))

        lines = []
        a = lines.append

        # ── Estimate label ────────────────────────────────────────────────────
        if is_estimate:
            a('<hr class="ta-sep-bold">')
            a(f'<div style="text-align:center;font-weight:700">{esc(L["estimate"])}</div>')
        if is_normal:
            a('<hr class="ta-sep-bold">')
            shop_name = shop.get('name_ta') or shop.get('name_en') or ''
            a(f'<div style="text-align:center;font-weight:700;font-size:12px">{esc(shop_name)}</div>')
            a('<hr class="ta-sep-bold">')

            for addr_key in ('address_line1_ta', 'address_line2_ta', 'address_line3_ta'):
                val = (shop.get(addr_key) or '').strip()
                if val:
                    a(f'<div style="text-align:center;font-size:9px">{esc(val)}</div>')

            phone = (shop.get('phone') or '').strip()
            if phone:
                a(f'<div style="text-align:center;font-size:9px">{esc(L["phone"])} {esc(phone)}</div>')

        # ── Bill metadata — 4-column table, no flex ──────────────────────────
        a('<hr class="ta-sep-thin">')
        a('<table class="ta-meta-table" style="font-size:9px">')
        
        a(f'<tr><td class="label">{esc(L["bill_no"])}</td>'
          f'<td class="value" colspan="3">{esc(bill.get("id", ""))}</td></tr>')
        
        date_val = (bill.get('date') or '').strip()
        time_val = self._format_time_display(bill.get('time'))
        if time_val:
            a(f'<tr><td class="label">{esc(L["date"])}</td><td class="value" style="white-space:nowrap">{esc(date_val)}</td>'
              f'<td class="label" style="padding-left:5px">{esc(L["time"])}</td><td class="value" style="white-space:nowrap">{esc(time_val)}</td></tr>')
        else:
            a(f'<tr><td class="label">{esc(L["date"])}</td><td class="value" colspan="3" style="white-space:nowrap">{esc(date_val)}</td></tr>')
            
        cust_name = bill.get('customer_name')
        cust_phone = bill.get('customer_phone')
        if cust_name and cust_phone:
            a(f'<tr><td class="label">{esc(L["customer"])}</td><td class="value">{esc(cust_name)}</td>'
              f'<td class="label" style="padding-left:5px">{esc(L["phone_cust"])}</td><td class="value" style="white-space:nowrap">{esc(cust_phone)}</td></tr>')
        elif cust_name:
            a(f'<tr><td class="label">{esc(L["customer"])}</td><td class="value" colspan="3">{esc(cust_name)}</td></tr>')
        elif cust_phone:
            a(f'<tr><td class="label" colspan="2"></td>'
              f'<td class="label" style="padding-left:5px">{esc(L["phone_cust"])}</td><td class="value" style="white-space:nowrap">{esc(cust_phone)}</td></tr>')

        a('</table>')
        a('<hr class="ta-sep-thin">')

        # ── Items table ────────────────────────────────────────────────────────
        a('<table class="ta-items-table">')
        a('<thead><tr>'
          f'<th>{esc(L["no"])}</th>'
          f'<th>{esc(L["product"])}</th>'
          f'<th>{esc(L["qty"])}</th>'
          f'<th>{esc(L["amt"])}</th>'
          '</tr></thead>')
        a('<tbody>')
        for i, it in enumerate(bill.get('items') or [], 1):
            name = self._item_product_name(it, tamil=True)
            qty_s = self._format_qty(it.get('qty'))
            amt_s = self._format_amt_money(it.get('amount'))
            a(f'<tr>'
              f'<td>{esc(i)}</td>'
              f'<td>{esc(name)}</td>'
              f'<td style="text-align:center">{esc(qty_s)}</td>'
              f'<td style="text-align:right;padding-right:0;white-space:nowrap">{esc(amt_s)}</td>'
              f'</tr>')
        a('</tbody></table>')

        # ── Summary (items count / qty) ────────────────────────────────────────
        a('<hr class="ta-sep-thin">')
        n   = int(bill.get('total_items') or len(bill.get('items') or []))
        qty = self._format_qty(bill.get('total_qty'))
        a(f'<div style="display:flex; justify-content:space-between; width:100%; font-size:10px;">')
        a(f'  <div><span>{esc(L["items"])}</span> : <span>{esc(n)}</span></div>')
        a(f'  <div><span>{esc(L["items_qty"])}</span> : <span>{esc(qty)}</span></div>')
        a(f'</div>')

        # ── Total ─────────────────────────────────────────────────────────────
        a('<hr class="ta-sep-bold">')
        total_s = self._money(bill.get('total', 0))
        a(f'<div class="ta-total-row"><span>{esc(L["total"])}</span><span>{esc(total_s)}</span></div>')
        a('<hr class="ta-sep-bold">')

        # ── Payment ───────────────────────────────────────────────────────────
        paid_s = self._money(bill.get('paid', 0))
        a(f'<div class="ta-payment-row"><span>{esc(L["paid"])}</span><span>{esc(paid_s)}</span></div>')
        returned = float(bill.get('returned', 0) or 0)
        if returned > 0:
            ret_s = self._money(returned)
            a(f'<div class="ta-payment-row"><span>{esc(L["returned"])}</span><span>{esc(ret_s)}</span></div>')

        # ── Nutrition ─────────────────────────────────────────────────────────
        nutr_rows = []
        for key, label in (
            ('protein',            L['protein']),
            ('absorbable_protein', L['abs_protein']),
            ('fat',                L['fat']),
            ('carbs',              L['carbs']),
            ('fiber',              L['fiber']),
        ):
            val = float(nutr.get(key, 0) or 0)
            if key == 'absorbable_protein' and val <= 0:
                continue
            nutr_rows.append((label, val))
        if is_oil:
            for key, label in (('moisture', L['moisture']), ('ash', L['ash'])):
                val = float(nutr.get(key, 0) or 0)
                if val > 0:
                    nutr_rows.append((label, val))
        else:
            ash_val = float(nutr.get('ash', 0) or 0)
            if ash_val > 0:
                nutr_rows.append((L['ash'], ash_val))

        if nutr_rows:
            a('<hr class="ta-sep-thin">')
            a(f'<div style="text-align:center;font-weight:700">------ {esc(L["nutrition"])} ------</div>')
            a('<hr class="ta-sep-thin">')
            for label, val in nutr_rows:
                a(f'<div class="ta-nutr-row"><span>{esc(label)}</span><span>{val:.1f}%</span></div>')
            a('<hr class="ta-sep-thin">')

        # ── Footer ────────────────────────────────────────────────────────────
        stars = '******** '
        a(f'<div style="text-align:center;font-size:9px">{stars}{esc(L["thanks"])}{stars[::-1]}</div>')
        a('<hr class="ta-sep-bold">')

        return '\n'.join(lines)


# ── Module-level convenience functions (single source of truth) ───────────────

def generate_receipt_text(ctx):
    """
    THE single source of truth for all receipt output.

    Preview, PDF (browser-print), ESC/POS thermal, and QZ Tray must all
    call this function and use the returned string.  Any code that formats
    receipt text independently is a duplicate that will drift out of sync.

    Returns the complete receipt as a single newline-joined string.
    """
    return '\n'.join(ReceiptFormatter().format_receipt(ctx))


def format_receipt_lines(ctx, chars_per_line=None):
    """Single source of truth — ALL renderers must use this."""
    return ReceiptFormatter(chars_per_line=chars_per_line).format_receipt(ctx)


def format_receipt_text(ctx, chars_per_line=None):
    return '\n'.join(format_receipt_lines(ctx, chars_per_line=chars_per_line))
