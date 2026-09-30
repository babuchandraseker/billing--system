"""
Direct thermal printing — USB and Windows raw spooler.
No browser print dialog.
"""

import logging
import sys
import threading

from .escpos_generator import EscposGenerator
from .receipt_formatter import format_receipt_lines, format_receipt_text

logger = logging.getLogger('billing.printer')

# pyusb write timeout. A long Tamil raster receipt is ~200 KB and a slow
# 80mm printer takes ~5 s to accept it, so 12 s only trips on a stuck device.
USB_WRITE_TIMEOUT_MS = 12000
# Hard limit for one whole send (open + write + close) on any transport. Kept
# below the POS screen's 20 s limit so the cashier sees the server's reason.
PRINT_DEADLINE_S = 15
# How long a new print waits for a previous send that is still running.
PRINT_LOCK_WAIT_S = 2

# One send at a time. A send that hit the deadline keeps holding this until its
# driver call returns, so nothing can be sent behind it (no duplicate receipt).
_send_lock = threading.Lock()


class PrintError(RuntimeError):
    """A print failure with a readable message and a machine-readable code."""
    code = 'printer_error'
    stage = 'printer'


class PrinterConfigError(PrintError):
    code = 'invalid_settings'
    stage = 'config'


class PrinterUnavailableError(PrintError):
    code = 'printer_unavailable'


class PrintTimeoutError(PrintError):
    code = 'timeout'


class PrinterBusyError(PrintError):
    code = 'printer_busy'


class UsbPrintError(PrintError):
    code = 'usb_error'


# Windows spooler states that mean a job would sit in the queue instead of
# printing (and could come out later, after the cashier printed again).
# Paper-out / error / cover-open are NOT here: drivers often leave those flags
# stale, and blocking on a stale flag would make printing impossible.
_WIN_STATUS_BLOCKING = (
    (0x00000080, 'is OFFLINE'),                 # PRINTER_STATUS_OFFLINE
    (0x00001000, 'is NOT AVAILABLE'),           # PRINTER_STATUS_NOT_AVAILABLE
    (0x00000001, 'is PAUSED in Windows'),       # PRINTER_STATUS_PAUSED
    (0x00000004, 'is being deleted'),           # PRINTER_STATUS_PENDING_DELETION
)
_WIN_ATTRIBUTE_WORK_OFFLINE = 0x00000400        # "Use Printer Offline" is ticked
_WIN_ERROR_INVALID_PRINTER_NAME = 1801


def check_windows_printer_ready(printer_name):
    """Raise PrinterUnavailableError when Windows reliably reports the printer
    as missing, offline or paused. Returns quietly when it is ready OR when its
    state cannot be determined — an unknown state is never reported as offline.
    """
    try:
        import win32print
    except ImportError:
        return

    try:
        handle = win32print.OpenPrinter(printer_name)
    except Exception as e:
        winerror = getattr(e, 'winerror', None)
        if winerror is None and getattr(e, 'args', None) and isinstance(e.args[0], int):
            winerror = e.args[0]
        if winerror == _WIN_ERROR_INVALID_PRINTER_NAME:
            raise PrinterUnavailableError(
                f'Windows printer {printer_name!r} is not installed on this computer. '
                'Check the printer name in Admin → Printer Settings.'
            ) from e
        logger.warning('Could not open %r to check its status; printing anyway', printer_name, exc_info=True)
        return

    try:
        info = win32print.GetPrinter(handle, 2)
    except Exception:
        logger.warning('Could not read the status of %r; printing anyway', printer_name, exc_info=True)
        return
    finally:
        try:
            win32print.ClosePrinter(handle)
        except Exception:
            pass

    status = int(info.get('Status') or 0)
    attributes = int(info.get('Attributes') or 0)
    problems = [text for flag, text in _WIN_STATUS_BLOCKING if status & flag]
    if attributes & _WIN_ATTRIBUTE_WORK_OFFLINE:
        problems.append('is set to "Use Printer Offline"')
    if problems:
        raise PrinterUnavailableError(
            f'Windows printer {printer_name!r} {" and ".join(problems)}. Nothing was sent. '
            'Turn the printer on, check the cable, and clear "Pause printing" / '
            '"Use Printer Offline" in Windows Settings → Printers, then print again.'
        )
    if status:
        logger.warning('Printer %r reports status 0x%x; printing anyway', printer_name, status)


def _is_usb_timeout(exc):
    try:
        import usb.core
        if isinstance(exc, usb.core.USBTimeoutError):
            return True
    except ImportError:
        pass
    return getattr(exc, 'errno', None) in (110, 10060)   # ETIMEDOUT / WSAETIMEDOUT


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
                'stage': 'receipt',
                'error_code': 'receipt_error',
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
                'stage': getattr(e, 'stage', 'printer'),
                'error_code': getattr(e, 'code', 'printer_error'),
                'message': str(e) or f'{type(e).__name__} (the printer driver gave no message)',
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

    def _resolve_transport(self):
        """Return ('usb', vid, pid) or ('win32', printer_name) — exactly one.

        Strict: there is no fallback to the Windows default printer, and an
        ambiguous configuration (USB IDs *and* a printer name) is an error
        rather than a silent choice.
        """
        printer_name = (self.settings.get('printer_name') or '').strip()
        vid = (self.settings.get('usb_vendor_id') or '').strip()
        pid = (self.settings.get('usb_product_id') or '').strip()

        if bool(vid) != bool(pid):
            raise PrinterConfigError(
                'USB Vendor ID and USB Product ID must both be set, or both left empty '
                '(Admin → Printer Settings).'
            )
        if vid and printer_name:
            raise PrinterConfigError(
                f'Both a USB printer ({vid}:{pid}) and a Windows printer ({printer_name!r}) are set. '
                'Clear one of them in Admin → Printer Settings so only one printer is used.'
            )
        if vid:
            return ('usb', vid, pid)
        if printer_name:
            return ('win32', printer_name)
        raise PrinterConfigError(
            'No thermal printer configured. Open Admin → Printer Settings and select the '
            'Windows printer name, or enter the USB Vendor ID and Product ID. '
            'The Windows default printer is never used automatically.'
        )

    def _send_raw(self, data):
        """Dispatch raw bytes to the one configured USB or Windows printer."""
        if not data:
            raise RuntimeError('Empty print data')

        transport = self._resolve_transport()
        if transport[0] == 'usb':
            self._run_with_deadline(lambda: self._print_usb(transport[1], transport[2], data),
                                    f'USB printer {transport[1]}:{transport[2]}')
        else:
            # Offline / paused / missing printers are refused BEFORE a job is
            # queued — Windows would otherwise accept it and print it later.
            check_windows_printer_ready(transport[1])
            self._run_with_deadline(lambda: self._print_win32(transport[1], data),
                                    f'Windows printer {transport[1]!r}')

    @staticmethod
    def _run_with_deadline(send, label):
        """Run one send in a worker thread and stop waiting after PRINT_DEADLINE_S.

        A driver call cannot be killed, so a timed-out worker keeps _send_lock
        until it returns: later prints get PrinterBusyError instead of queueing a
        second copy behind it. Nothing is ever retried here.
        """
        if not _send_lock.acquire(timeout=PRINT_LOCK_WAIT_S):
            raise PrinterBusyError(
                f'The previous receipt is still being sent to the {label} (it did not respond). '
                'Nothing new was sent. Check the printer and the paper, wait a few seconds, '
                'then print again.'
            )
        outcome = {}

        def work():
            try:
                send()
            except BaseException as e:
                outcome['error'] = e
            finally:
                _send_lock.release()

        worker = threading.Thread(target=work, name='thermal-print', daemon=True)
        worker.start()
        worker.join(PRINT_DEADLINE_S)
        if worker.is_alive():
            logger.error('Print to %s still running after %ss — stopped waiting', label, PRINT_DEADLINE_S)
            raise PrintTimeoutError(
                f'The {label} did not finish printing within {PRINT_DEADLINE_S} seconds, so the app '
                'stopped waiting. The receipt may be incomplete, or may still come out late. '
                'Check the printer and the paper BEFORE printing again.'
            )
        if 'error' in outcome:
            raise outcome['error']

    def _print_usb(self, vid, pid, data):
        from escpos.printer import Usb
        vendor = int(vid, 16)
        product = int(pid, 16)
        # timeout: without it pyusb waits forever on a stuck printer.
        p = Usb(vendor, product, timeout=USB_WRITE_TIMEOUT_MS)
        try:
            if isinstance(data, str):
                data = data.encode('utf-8', errors='replace')
            p._raw(data)
        except Exception as e:
            if _is_usb_timeout(e):
                raise PrintTimeoutError(
                    f'USB printer {vid}:{pid} did not accept the receipt within '
                    f'{USB_WRITE_TIMEOUT_MS // 1000} seconds ({e}). The receipt may be incomplete. '
                    'Check the printer (paper, cover, power) BEFORE printing again.'
                ) from e
            raise UsbPrintError(
                f'USB printer {vid}:{pid} failed: {str(e) or type(e).__name__}'
            ) from e
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
        except ImportError:
            Win32Raw = None

        if Win32Raw is not None:
            p = Win32Raw(printer_name)
            try:
                p._raw(data)
            except Exception:
                # The write failed: release the half-open job, but report the
                # ORIGINAL error — a cleanup failure must not replace it.
                try:
                    p.close()
                except Exception:
                    logger.warning('Win32Raw cleanup after failed write also failed', exc_info=True)
                raise
            # close() ends the job (EndPagePrinter / EndDocPrinter). If Windows
            # rejects it here the job was not committed, so the error must reach
            # the user — it is deliberately NOT swallowed.
            p.close()
            return

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

    def get_active_transport(self):
        """Return the transport identifier configured for the printer."""
        mode = (self.settings.get('mode') or 'escpos').lower()
        if mode in ('preview', 'browser'):
            return f'preview ({mode})'
        try:
            transport = self._resolve_transport()
        except PrinterConfigError:
            return 'not configured'
        if transport[0] == 'usb':
            return f'usb ({transport[1]}:{transport[2]})'
        return f'win32raw ({transport[1]})'
