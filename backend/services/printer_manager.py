"""
Printer settings persistence and Windows printer discovery.
"""

import json
import logging
import subprocess
import sys

logger = logging.getLogger('billing.printer')

PRINTER_SETTINGS_KEY = 'printer_settings'

DEFAULT_SETTINGS = {
    'mode': 'escpos',           # escpos | qz | browser | preview
    'paper_width_mm': 80,
    # Font A (12×24 dots) on 80mm: 576 / 12 = 48 columns.
    # Font B  (9×17 dots) on 80mm: 576 /  9 = 64 columns (too dense for readability).
    # ESC M 0 (Font A) is injected in escpos_generator.py before every receipt.
    'chars_per_line': 48,
    'font_mode': 'a',           # 'a' = Font A 48-col on 80mm (default) | 'b' = Font B 64-col
    'font_size': 'normal',      # small | normal | large
    'printer_name': '',
    'usb_vendor_id': '',
    'usb_product_id': '',
    'auto_cut': True,
    'encoding': 'utf-8',
    'qz_tray_enabled': False,
    'qz_printer_name': '',
}

# Print modes the user can explicitly choose. Anything else is a configuration
# error — it is never silently treated as one of these.
VALID_MODES = ('escpos', 'qz', 'browser', 'preview')
VALID_PAPER_WIDTHS_MM = (58, 80)


def validate_settings(settings):
    """Return a list of configuration errors (empty list = valid).

    Strict: the configured mode and printer are used exactly as saved. There is
    no fallback to another mode or to the Windows default printer, so an
    incomplete or ambiguous configuration must be rejected up front.
    """
    s = settings or {}
    errors = []

    mode = s.get('mode')
    if mode not in VALID_MODES:
        errors.append(f'Invalid print mode {mode!r}. Choose one of: {", ".join(VALID_MODES)}.')

    try:
        paper = int(s.get('paper_width_mm'))
    except (TypeError, ValueError):
        paper = None
    if paper not in VALID_PAPER_WIDTHS_MM:
        errors.append(f'Invalid paper width {s.get("paper_width_mm")!r}. Choose 58 or 80 (mm).')

    vid = str(s.get('usb_vendor_id') or '').strip()
    pid = str(s.get('usb_product_id') or '').strip()
    if bool(vid) != bool(pid):
        errors.append('USB Vendor ID and USB Product ID must both be set, or both left empty.')
    elif vid:
        try:
            int(vid, 16)
            int(pid, 16)
        except ValueError:
            errors.append(f'USB IDs must be hexadecimal (e.g. 04b8): got {vid!r}:{pid!r}.')

    printer_name = str(s.get('printer_name') or '').strip()
    if mode == 'escpos':
        if vid and pid and printer_name:
            errors.append(
                f'Both a USB printer ({vid}:{pid}) and a Windows printer ({printer_name!r}) are set. '
                'Clear one of them so only one printer is used.'
            )
        elif not (vid and pid) and not printer_name:
            errors.append(
                'No thermal printer configured. Select the Windows printer name, '
                'or enter the USB Vendor ID and Product ID.'
            )
    if mode == 'qz' and not str(s.get('qz_printer_name') or '').strip():
        errors.append('QZ Tray mode needs a QZ printer name.')

    return errors


class PrinterManager:
    """Load/save printer preferences from app_settings."""

    def __init__(self, get_db):
        self._get_db = get_db

    def load_settings(self):
        conn = self._get_db()
        try:
            row = conn.execute(
                'SELECT value FROM app_settings WHERE key=?',
                (PRINTER_SETTINGS_KEY,)
            ).fetchone()
            if not row:
                return dict(DEFAULT_SETTINGS)
            raw = row['value'] if hasattr(row, 'keys') else row[0]
            data = json.loads(raw) if raw else {}
            merged = dict(DEFAULT_SETTINGS)
            merged.update(data)
            return merged
        except Exception as e:
            logger.warning('Failed to load printer settings: %s', e)
            return dict(DEFAULT_SETTINGS)
        finally:
            conn.close()

    def save_settings(self, settings):
        merged = dict(DEFAULT_SETTINGS)
        merged.update(settings or {})
        conn = self._get_db()
        try:
            conn.execute(
                'INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)',
                (PRINTER_SETTINGS_KEY, json.dumps(merged))
            )
            conn.commit()
            return merged
        finally:
            conn.close()

    @staticmethod
    def list_printers():
        """Return installed printer names (Windows-first)."""
        printers = []
        if sys.platform == 'win32':
            try:
                import win32print
                flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
                for p in win32print.EnumPrinters(flags):
                    name = p[2] if len(p) > 2 else str(p)
                    if name and name not in printers:
                        printers.append(name)
                if printers:
                    return sorted(printers)
            except ImportError:
                pass
            except Exception as e:
                logger.debug('win32print enumeration failed: %s', e)

            try:
                result = subprocess.run(
                    ['wmic', 'printer', 'get', 'name'],
                    capture_output=True, text=True, timeout=8,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                )
                for line in (result.stdout or '').splitlines():
                    name = line.strip()
                    if name and name.lower() != 'name' and name not in printers:
                        printers.append(name)
            except Exception as e:
                logger.debug('wmic printer list failed: %s', e)

        return sorted(printers)

    @staticmethod
    def detect_usb_devices():
        """Best-effort USB device listing for ESC/POS USB mode."""
        devices = []
        try:
            import usb.core
            import usb.util
            for dev in usb.core.find(find_all=True):
                try:
                    vid = f'{dev.idVendor:04x}'
                    pid = f'{dev.idProduct:04x}'
                    label = f'USB {vid}:{pid}'
                    try:
                        if dev.iManufacturer and dev.iProduct:
                            m = usb.util.get_string(dev, dev.iManufacturer)
                            p = usb.util.get_string(dev, dev.iProduct)
                            label = f'{m} {p} ({vid}:{pid})'
                    except Exception:
                        pass
                    devices.append({
                        'vendor_id': vid,
                        'product_id': pid,
                        'label': label,
                    })
                except Exception:
                    continue
        except ImportError:
            logger.debug('pyusb not installed — USB auto-detect skipped')
        except Exception as e:
            logger.debug('USB scan failed: %s', e)
        return devices

    @staticmethod
    def default_windows_printer():
        if sys.platform != 'win32':
            return ''
        try:
            import win32print
            return win32print.GetDefaultPrinter() or ''
        except Exception:
            return ''

    def ensure_defaults(self):
        """First-run: 80mm / 48 chars.

        Never fills in printer_name from the Windows default printer — that
        silently chose a printer the user never selected (e.g. an office
        laser or "Microsoft Print to PDF"). The printer must be chosen
        explicitly in Admin → Printer Settings.
        """
        settings = self.load_settings()
        changed = False
        if not settings.get('chars_per_line'):
            settings['chars_per_line'] = 48  # Font A: 48 cols on 80mm paper
            changed = True
        if changed:
            self.save_settings(settings)
        return settings
