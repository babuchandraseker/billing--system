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
        """First-run: pick default Windows printer and 80mm / 48 chars."""
        settings = self.load_settings()
        changed = False
        if not (settings.get('printer_name') or '').strip():
            default = self.default_windows_printer()
            if default:
                settings['printer_name'] = default
                changed = True
        if not settings.get('chars_per_line'):
            settings['chars_per_line'] = 48  # Font A: 48 cols on 80mm paper
            changed = True
        if changed:
            self.save_settings(settings)
        return settings
