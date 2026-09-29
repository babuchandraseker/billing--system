"""
Direct thermal printing — USB and Windows raw spooler.
No browser print dialog.
"""

import logging
import sys

from .escpos_generator import EscposGenerator
from .receipt_formatter import format_receipt_lines, format_receipt_text

logger = logging.getLogger('billing.printer')


class ThermalPrinterService:
    """Print receipts via ESC/POS to configured device."""

    def __init__(self, settings=None):
        self.settings = settings or {}

    def build_raw_receipt(self, ctx):
        """Return (lines, raw_bytes, preview_text) without sending to printer."""
        lines = format_receipt_lines(ctx)
        preview = format_receipt_text(ctx)

        # ── DEBUG: log ESC/POS and QZ Tray receipt path ───────────────────────
        import logging as _logging
        _log = _logging.getLogger('billing.printer')
        _log.debug('ESC/POS RECEIPT: chars_per_line=48  formatter=format_receipt_lines  lines=%d', len(lines))
        for ln in lines:
            _log.debug('ESC/POS RECEIPT LINE: %r', ln)

        generator = EscposGenerator(self.settings)
        language = str(ctx.get('language') or 'english').lower()
        raw = generator.generate_receipt_bytes(
            lines,
            rasterize_unicode=language in ('ta', 'tamil'),
            ctx=ctx,
        )
        return lines, raw, preview

    def print_receipt(self, ctx):
        """
        Format context, generate ESC/POS, send to printer.
        Returns dict with success, message, preview (optional).
        """
        try:
            lines, raw, preview = self.build_raw_receipt(ctx)
        except Exception as e:
            logger.exception('ESC/POS generation failed')
            return {
                'success': False,
                'message': f'ESC/POS generation failed: {e}',
                'preview': '',
            }

        try:
            self._send_raw(raw)
            logger.info('Thermal print OK bill=%s', ctx.get('bill', {}).get('id'))
            return {
                'success': True,
                # Spooler/USB accepted the data - not proof that paper came out
                # (paper-out / cover-open are not reported back to us).
                'message': 'Sent to printer',
                'preview': preview,
            }
        except Exception as e:
            logger.exception('Thermal print failed')
            return {
                'success': False,
                'message': str(e),
                'preview': preview,
            }

    def print_test_receipt(self, language='english'):
        """Print a sample receipt for configuration testing.

        Pass language='tamil' to test Tamil ESC/POS raster printing.
        """
        lang = 'tamil' if str(language or '').lower() in ('ta', 'tamil') else 'english'
        sample_ctx = {
            'language': lang,
            'is_estimate': False,
            'is_normal': True,
            'is_oil_product': False,
            'shop': {
                'name_en': 'DHANA DHANIYA KADAI',
                'name_ta': 'தன தானிய கடை',
                'address_line1': 'No.16 Thirumeni Nagar',
                'address_line2': 'School Road, Kolathur, Chennai-600099',
                'address_line3': '',
                'address_line1_ta': 'எண்.16 திருமேனி நகர்',
                'address_line2_ta': 'ஸ்கூல் ரோடு, கொளத்தூர், சென்னை-600099',
                'address_line3_ta': '',
                'phone': '9940116970, 9944308925',
            },
            'bill': {
                'id': '89',
                'date': '25/05/2026',
                'time': '09:15:00 PM',
                'customer_name': 'Vishnu',
                'customer_phone': '9962448009',
                'items': [
                    {'name': 'SNOW BARNYARD MILLET', 'name_ta': 'பனி குதிரைவாலி', 'qty': 1, 'rate': 75.0, 'amount': 75.0},
                    {'name': 'RETAIL PACK', 'name_ta': 'சில்லறை', 'qty': 1, 'rate': 100.0, 'amount': 100.0},
                ],
                'total_items': 2,
                'total_qty': 2,
                'subtotal': 175.0,
                'total': 175.0,
                'paid': 175.0,
                'returned': 0.0,
            },
            'nutrition': {
                'protein': 9.0,
                'absorbable_protein': 5.8,
                'fat': 2.9,
                'carbs': 67.5,
                'fiber': 9.2,
                'moisture': 0,
                'ash': 0,
            },
        }
        return self.print_receipt(sample_ctx)

    def _send_raw(self, data):
        """Dispatch raw bytes to USB or Windows printer."""
        if not data:
            raise RuntimeError('Empty print data')

        printer_name = (self.settings.get('printer_name') or '').strip()
        vid = (self.settings.get('usb_vendor_id') or '').strip()
        pid = (self.settings.get('usb_product_id') or '').strip()

        if vid and pid:
            self._print_usb(vid, pid, data)
            return

        if printer_name:
            self._print_win32(printer_name, data)
            return

        if sys.platform == 'win32':
            default = self._default_windows_printer()
            if default:
                self._print_win32(default, data)
                return

        raise RuntimeError(
            'No printer configured. Set printer name in Admin → Printer Settings.'
        )

    def _print_usb(self, vid, pid, data):
        from escpos.printer import Usb
        vendor = int(vid, 16)
        product = int(pid, 16)
        p = Usb(vendor, product)
        try:
            if isinstance(data, str):
                data = data.encode('utf-8', errors='replace')
            p._raw(data)
        finally:
            try:
                p.close()
            except Exception:
                pass

    def _print_win32(self, printer_name, data):
        if isinstance(data, str):
            data = data.encode('utf-8', errors='replace')

        try:
            from escpos.printer import Win32Raw
            p = Win32Raw(printer_name)
            try:
                p._raw(data)
            finally:
                try:
                    p.close()
                except Exception:
                    pass
            return
        except ImportError:
            pass

        if sys.platform != 'win32':
            raise RuntimeError('Win32Raw printing requires Windows')

        import win32print
        h = win32print.OpenPrinter(printer_name)
        try:
            win32print.StartDocPrinter(h, 1, ('Thermal Receipt', None, 'RAW'))
            try:
                win32print.StartPagePrinter(h)
                win32print.WritePrinter(h, data)
                win32print.EndPagePrinter(h)
            finally:
                win32print.EndDocPrinter(h)
        finally:
            win32print.ClosePrinter(h)

    @staticmethod
    def _default_windows_printer():
        if sys.platform != 'win32':
            return None
        try:
            import win32print
            return win32print.GetDefaultPrinter()
        except Exception:
            return None

    def get_active_transport(self):
        """Return the transport identifier configured for the printer."""
        mode = (self.settings.get('mode') or 'escpos').lower()
        if mode in ('preview', 'browser'):
            return f'preview ({mode})'
        vid = (self.settings.get('usb_vendor_id') or '').strip()
        pid = (self.settings.get('usb_product_id') or '').strip()
        if vid and pid:
            return f'usb ({vid}:{pid})'
        printer_name = (self.settings.get('printer_name') or '').strip()
        if printer_name:
            return f'win32raw ({printer_name})'
        if sys.platform == 'win32':
            default = self._default_windows_printer()
            if default:
                return f'win32raw (default: {default})'
        return 'escpos_direct'
