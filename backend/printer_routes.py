"""
Thermal printer API routes — keeps app.py lean.
"""

import base64
import logging
from flask import jsonify, request, render_template

from services.printer_manager import PrinterManager
from services.receipt_formatter import ReceiptFormatter, format_receipt_lines, format_receipt_text
from services.thermal_printer import ThermalPrinterService

logger = logging.getLogger('billing.printer')


def _debug_dir():
    """Writable diagnostics folder, independent of the process cwd.
    Dev: <project>/debug_output. PyInstaller EXE: next to the .exe."""
    import os, sys
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, 'debug_output')
    os.makedirs(path, exist_ok=True)
    return path


def _physical_status(meta, validation, current_transport):
    """PHYSICAL PRINTER VALIDATION is PASS only when an operator confirmed the
    paper from the LATEST successfully-sent test on the CURRENT transport."""
    if not validation or validation.get('physical_printer_validation') != 'PASS':
        return 'PENDING'
    if not meta or meta.get('print_attempt_status') != 'SENT':
        return 'PENDING'
    if validation.get('test_timestamp') != meta.get('timestamp'):
        return 'PENDING'
    if validation.get('printer_transport') != current_transport:
        return 'PENDING'
    return 'PASS'


def register_printer_routes(app, get_db, build_print_context_fn, fetch_bill_row_fn):
    """Register /api/printer/* routes on the Flask app."""

    def _manager():
        return PrinterManager(get_db)

    def _receipt_language(value):
        return 'tamil' if str(value or '').lower() in ('ta', 'tamil') else 'english'

    @app.route('/api/printer/settings', methods=['GET'])
    def printer_settings_get():
        mgr = _manager()
        settings = mgr.ensure_defaults()
        return jsonify({
            'success': True,
            'settings': settings,
            'printers': PrinterManager.list_printers(),
            'usb_devices': PrinterManager.detect_usb_devices(),
        })

    @app.route('/api/printer/settings', methods=['POST'])
    def printer_settings_save():
        data = request.get_json(silent=True) or {}
        mgr = _manager()
        saved = mgr.save_settings(data.get('settings') or data)
        return jsonify({'success': True, 'settings': saved})

    @app.route('/api/printer/detect', methods=['GET'])
    def printer_detect():
        return jsonify({
            'success': True,
            'printers': PrinterManager.list_printers(),
            'usb_devices': PrinterManager.detect_usb_devices(),
        })

    @app.route('/api/printer/test', methods=['POST'])
    def printer_test():
        data = request.get_json(silent=True) or {}
        language = _receipt_language(data.get('language', 'english'))
        mgr = _manager()
        settings = mgr.load_settings()
        svc = ThermalPrinterService(settings)
        result = svc.print_test_receipt(language=language)
        result['language'] = language
        status = 200 if result.get('success') else 500
        return jsonify(result), status

    @app.route('/api/printer/preview', methods=['POST'])
    def printer_preview():
        data = request.get_json(silent=True) or {}
        bill_id = data.get('bill_id')
        language = _receipt_language(data.get('language', 'english'))
        bill_type = data.get('bill_type', 'normal')
        paid = data.get('paid_amount')
        paid_override = round(float(paid), 2) if paid is not None else None

        row = fetch_bill_row_fn(bill_id)
        if not row:
            return jsonify({'success': False, 'message': f'Bill #{bill_id} not found'}), 404

        ctx = build_print_context_fn(row, language, bill_type, paid_amount_override=paid_override)
        ctx['language'] = language
        lines = format_receipt_lines(ctx)
        text = '\n'.join(lines)

        # ── DEBUG: log preview receipt path ───────────────────────────────────
        logger.debug('PREVIEW RECEIPT: chars_per_line=48  formatter=ReceiptFormatter  lines=%d', len(lines))
        for ln in lines:
            logger.debug('PREVIEW RECEIPT LINE: %r', ln)

        return jsonify({'success': True, 'preview': text, 'formatted_lines': lines, 'context': {
            'bill_id': bill_id,
            'language': language,
            'bill_type': bill_type,
        }})

    @app.route('/api/thermal/print', methods=['POST'])
    def thermal_print_bill():
        """Direct ESC/POS print — no browser dialog."""
        data = request.get_json(silent=True) or {}
        bill_id = data.get('bill_id')
        if not bill_id:
            return jsonify({'success': False, 'message': 'bill_id required'}), 400

        language = _receipt_language(data.get('language', 'english'))
        bill_type = data.get('bill_type', 'normal')
        paid = data.get('paid_amount')
        paid_override = round(float(paid), 2) if paid is not None else None

        mgr = _manager()
        settings = mgr.load_settings()
        mode = (settings.get('mode') or 'escpos').lower()

        row = fetch_bill_row_fn(bill_id)
        if not row:
            return jsonify({'success': False, 'message': f'Bill #{bill_id} not found'}), 404

        ctx = build_print_context_fn(row, language, bill_type, paid_amount_override=paid_override)
        ctx['language'] = language

        if mode == 'preview':
            chars = int(settings.get('chars_per_line', 48))
            lines = format_receipt_lines(ctx)
            return jsonify({
                'success': True,
                'mode': 'preview',
                'message': 'Preview only (no print)',
                'preview': '\n'.join(lines),
                'formatted_lines': lines,
            })

        if mode == 'browser':
            return jsonify({
                'success': True,
                'mode': 'browser',
                'message': 'Use browser print fallback',
                'print_url': (
                    f'/print-bill/{bill_id}?language={language}&bill_type={bill_type}'
                    + (f'&paid_amount={paid_override:.2f}' if paid_override is not None else '')
                    + '&preview=1'
                ),
                'thermal_preview_url': (
                    f'/preview-thermal/{bill_id}?language={language}&bill_type={bill_type}'
                    + (f'&paid_amount={paid_override:.2f}' if paid_override is not None else '')
                ),
            })

        if mode == 'qz':
            svc = ThermalPrinterService(settings)
            try:
                _, raw, preview = svc.build_raw_receipt(ctx)
                return jsonify({
                    'success': True,
                    'mode': 'qz',
                    'message': 'Send raw data via QZ Tray',
                    'preview': preview,
                    'raw_base64': base64.b64encode(raw).decode('ascii'),
                    'printer_name': settings.get('qz_printer_name') or settings.get('printer_name') or '',
                })
            except Exception as e:
                logger.exception('QZ raw build failed')
                return jsonify({'success': False, 'message': str(e)}), 500

        svc = ThermalPrinterService(settings)
        result = svc.print_receipt(ctx)
        result['mode'] = 'escpos'
        status = 200 if result.get('success') else 500
        if not result.get('success'):
            logger.error('Print failed bill=%s: %s', bill_id, result.get('message'))
        return jsonify(result), status

    @app.route('/admin/printer', methods=['GET'])
    def admin_printer_page():
        return render_template('printer_settings.html')

    @app.route('/api/printer/sample-receipt', methods=['GET'])
    def sample_receipt_text():
        """Text-only sample receipt for debugging (no physical print)."""
        mgr = _manager()
        settings = mgr.load_settings()
        chars = int(settings.get('chars_per_line', 48))
        language = _receipt_language(request.args.get('language', 'english'))
        sample_ctx = {
            'language': language,
            'is_estimate': False,
            'is_normal': True,
            'is_oil_product': False,
            'shop': {
                'name_en': 'DHANA DHANIYA KADAI',
                'name_ta': 'தன தானிய கடை',
                'phone': '9940116970, 9944308925',
                'address_line1': 'No.16 Thirumeni Nagar',
                'address_line2': 'School Road, Kolathur, Chennai-600099',
                'address_line3': '',
                'address_line1_ta': 'எண்.16 திருமேனி நகர்',
                'address_line2_ta': 'ஸ்கூல் ரோடு, கொளத்தூர், சென்னை-600099',
                'address_line3_ta': '',
            },
            'bill': {
                'id': '89',
                'date': '25/05/2026',
                'time': '09:15 PM',
                'customer_name': 'Vishnu',
                'customer_phone': '9962448009',
                'items': [
                    {'name': 'SNOW BARNYARD MILLET', 'tamil_name': 'பனிவரகு', 'qty': 1, 'rate': 75, 'amount': 75},
                    {'name': 'RETAIL PACK', 'tamil_name': 'சில்லறை பொதி', 'qty': 1, 'rate': 100, 'amount': 100},
                ],
                'total_items': 2, 'total_qty': 2, 'subtotal': 175, 'total': 175,
                'paid': 175, 'returned': 0,
            },
            'nutrition': {'protein': 12.5, 'fat': 3.2, 'carbs': 65, 'fiber': 8.1,
                          'absorbable_protein': 0, 'moisture': 0, 'ash': 0},
        }
        lines = format_receipt_lines(sample_ctx)
        return jsonify({
            'success': True,
            'language': language,
            'preview': '\n'.join(lines),
            'formatted_lines': lines,
        })

    @app.route('/api/thermal/raw', methods=['POST'])
    def thermal_raw_bytes():
        """Return base64 ESC/POS payload for QZ Tray or external tools."""
        data = request.get_json(silent=True) or {}
        bill_id = data.get('bill_id')
        if not bill_id:
            return jsonify({'success': False, 'message': 'bill_id required'}), 400

        language = _receipt_language(data.get('language', 'english'))
        bill_type = data.get('bill_type', 'normal')
        paid = data.get('paid_amount')
        paid_override = round(float(paid), 2) if paid is not None else None

        row = fetch_bill_row_fn(bill_id)
        if not row:
            return jsonify({'success': False, 'message': f'Bill #{bill_id} not found'}), 404

        mgr = _manager()
        settings = mgr.load_settings()
        ctx = build_print_context_fn(row, language, bill_type, paid_amount_override=paid_override)
        ctx['language'] = language

        try:
            svc = ThermalPrinterService(settings)
            _, raw, preview = svc.build_raw_receipt(ctx)
            return jsonify({
                'success': True,
                'preview': preview,
                'raw_base64': base64.b64encode(raw).decode('ascii'),
                'printer_name': settings.get('qz_printer_name') or settings.get('printer_name') or '',
            })
        except Exception as e:
            logger.exception('Raw receipt build failed')
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route('/api/printer/diagnostic-ruler', methods=['GET'])
    def printer_diagnostic_ruler():
        """
        Print a 48-char ruler to confirm the printer is using full 80mm width.
        The entire ruler MUST appear on a single line with no wrapping.
        If it wraps, the printer is still in narrow/58mm mode.

        Ruler:
          123456789012345678901234567890123456789012345678
          ^        ^         ^         ^         ^      ^
          1        10        20        30        40     48
        """
        mgr = _manager()
        settings = mgr.load_settings()
        paper_width = int(settings.get('paper_width_mm') or 80)
        chars = int(settings.get('chars_per_line') or 48)
        font_mode = (settings.get('font_mode') or 'a').upper()

        ruler = '123456789012345678901234567890123456789012345678'
        assert len(ruler) == 48, 'Ruler must be exactly 48 chars'

        # Build a diagnostic receipt with the ruler at the top
        lines = [
            '=' * chars,
            'PRINTER DIAGNOSTIC — 80mm WIDTH TEST',
            '=' * chars,
            f'Paper width  : {paper_width}mm',
            f'Chars/line   : {chars}',
            f'Font mode    : Font {font_mode}',
            f'Ruler (48ch) :',
            ruler,
            '-' * chars,
            'If the ruler above fits on ONE line without',
            'wrapping → printer is in 80mm / 48-col mode.',
            'If it wraps → ESC/POS condensed mode still',
            'active or wrong paper width in settings.',
            '=' * chars,
        ]

        logger.info(
            '[DIAGNOSTIC] paper=%dmm chars=%d font=%s ruler_len=%d',
            paper_width, chars, font_mode, len(ruler)
        )

        mode = (settings.get('mode') or 'escpos').lower()

        if mode in ('preview', 'browser'):
            return jsonify({
                'success': True,
                'mode': 'preview',
                'message': 'Diagnostic ruler (preview only — not printed)',
                'paper_width_mm': paper_width,
                'chars_per_line': chars,
                'font_mode': font_mode,
                'ruler': ruler,
                'ruler_length': len(ruler),
                'preview': '\n'.join(lines),
            })

        import base64
        try:
            from services.escpos_generator import EscposGenerator
            gen = EscposGenerator(settings)
            raw = gen.generate_receipt_bytes(lines, rasterize_unicode=False)
            svc = ThermalPrinterService(settings)
            result = svc._send_raw(raw)
            return jsonify({
                'success': result.get('success', True),
                'message': result.get('message', 'Diagnostic ruler sent to printer'),
                'paper_width_mm': paper_width,
                'chars_per_line': chars,
                'font_mode': font_mode,
                'ruler': ruler,
                'ruler_length': len(ruler),
                'preview': '\n'.join(lines),
                'raw_base64': base64.b64encode(raw).decode('ascii'),
            })
        except Exception as e:
            logger.exception('Diagnostic ruler print failed')
            return jsonify({
                'success': False,
                'message': str(e),
                'paper_width_mm': paper_width,
                'chars_per_line': chars,
                'font_mode': font_mode,
                'ruler': ruler,
                'preview': '\n'.join(lines),
            }), 500

    @app.route('/api/printer/info', methods=['GET'])
    def printer_info():
        """Return effective printer configuration for debugging."""
        mgr = _manager()
        settings = mgr.load_settings()
        paper_width = int(settings.get('paper_width_mm') or 80)
        chars = int(settings.get('chars_per_line') or 48)
        font_mode = (settings.get('font_mode') or 'a').upper()

        logger.info(
            '[PRINTER-INFO] paper=%dmm chars=%d font=Font%s mode=%s printer=%s',
            paper_width, chars, font_mode,
            settings.get('mode'), settings.get('printer_name')
        )

        return jsonify({
            'paper_width_mm': paper_width,
            'chars_per_line': chars,
            'font_mode': f'Font {font_mode}',
            'mode': settings.get('mode'),
            'printer_name': settings.get('printer_name'),
            'auto_cut': settings.get('auto_cut'),
            'expected_cols': {
                '80mm_font_a': 48,
                '80mm_font_b': 64,
                '58mm_font_a': 32,
                '58mm_font_b': 42,
            },
            'note': (
                f'80mm paper + Font A = 48 cols. '
                f'Current: {paper_width}mm + Font {font_mode} = {chars} cols'
            ),
        })


    @app.route('/debug/tamil-raster', methods=['GET'])
    def debug_tamil_raster():
        """
        Generates the Tamil raster bitmap and serves it as PNG in the browser.
        This shows EXACTLY what will print on thermal paper.
        URL: http://localhost:5000/debug/tamil-raster
        URL: http://localhost:5000/debug/tamil-raster?bill_id=89  (real bill)
        """
        import io
        from flask import send_file
        from services.tamil_text_render import TamilReceiptRasterRenderer

        mgr = _manager()
        settings = mgr.load_settings()
        bill_id = request.args.get('bill_id')
        language = 'tamil'

        if bill_id:
            row = fetch_bill_row_fn(bill_id)
            if not row:
                return f'Bill #{bill_id} not found', 404
            ctx = build_print_context_fn(row, language, 'normal')
            ctx['language'] = language
        else:
            ctx = {
                'language': 'tamil',
                'is_estimate': False,
                'is_normal': True,
                'is_oil_product': False,
                'shop': {
                    'name_en': 'DHANA DHANIYA KADAI',
                    'name_ta': 'தன தானிய கடை',
                    'phone': '9940116970, 9944308925',
                    'address_line1': 'No.16 Thirumeni Nagar',
                    'address_line2': 'School Road, Kolathur, Chennai-600099',
                    'address_line3': '',
                    'address_line1_ta': 'எண்.16 திருமேனி நகர்',
                    'address_line2_ta': 'ஸ்கூல் ரோடு, கொளத்தூர், சென்னை-600099',
                    'address_line3_ta': '',
                },
                'bill': {
                    'id': 'SAMPLE',
                    'date': '26/06/2026',
                    'time': '01:30 PM',
                    'customer_name': 'Vishnu',
                    'customer_phone': '9962448009',
                    'items': [
                        {'name': 'FOXTAIL MILLET', 'name_ta': 'திணை பாரம்பரிய தானியம்', 'qty': 2, 'rate': 55.0, 'amount': 110.0},
                        {'name': 'RED FOXTAIL MILLET', 'name_ta': 'செந்தினை இயற்கை முறை', 'qty': 1, 'rate': 90.0, 'amount': 90.0},
                        {'name': 'BARNYARD MILLET', 'name_ta': 'குதிரைவாலி சிறுதானிய அரிசி', 'qty': 1, 'rate': 80.0, 'amount': 80.0},
                    ],
                    'total_items': 3, 'total_qty': 4,
                    'subtotal': 280.0, 'total': 280.0,
                    'paid': 300.0, 'returned': 20.0,
                },
                'nutrition': {
                    'protein': 12.5, 'absorbable_protein': 0,
                    'fat': 3.2, 'carbs': 65.0, 'fiber': 8.1,
                    'moisture': 0, 'ash': 0,
                },
            }

        renderer = TamilReceiptRasterRenderer(settings)
        image = renderer.render_receipt_image(ctx)

        buf = io.BytesIO()
        image.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png', download_name='tamil_receipt_preview.png')

    @app.route('/debug/tamil-preview', methods=['GET'])
    def debug_tamil_preview():
        """
        Interactive browser test page displaying the actual production 576px Tamil receipt bitmap.
        Simulates physical 80mm thermal paper with high-fidelity raster display.
        """
        bill_id = request.args.get('bill_id', '')
        raster_src = f'/debug/tamil-raster?bill_id={bill_id}' if bill_id else '/debug/tamil-raster'
        html = f"""<!DOCTYPE html>
<html lang="ta">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tamil Thermal Receipt — Visual Simulator</title>
<style>
  body {{
    margin: 0;
    padding: 20px;
    background: #e2e8f0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex;
    flex-direction: column;
    align-items: center;
    min-height: 100vh;
  }}
  .toolbar {{
    background: #ffffff;
    padding: 14px 24px;
    border-radius: 12px;
    box-shadow: 0 4px 12px rgba(0,0,0,0.08);
    display: flex;
    gap: 12px;
    align-items: center;
    margin-bottom: 24px;
    flex-wrap: wrap;
  }}
  .toolbar h2 {{
    margin: 0;
    font-size: 16px;
    color: #1e293b;
    margin-right: 12px;
  }}
  .btn {{
    padding: 8px 16px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }}
  .btn-primary {{
    background: #166534;
    color: #ffffff;
    border: none;
  }}
  .btn-primary:hover {{
    background: #14532d;
  }}
  .btn-secondary {{
    background: #f1f5f9;
    color: #334155;
    border: 1px solid #cbd5e1;
  }}
  .btn-secondary:hover {{
    background: #e2e8f0;
  }}
  .paper-container {{
    background: #ffffff;
    padding: 10px 0;
    box-shadow: 0 10px 25px rgba(0,0,0,0.15), 0 2px 6px rgba(0,0,0,0.08);
    border-radius: 4px;
    width: 80mm;
    max-width: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    margin-bottom: 40px;
  }}
  .paper-container img {{
    width: 80mm;
    height: auto;
    display: block;
    image-rendering: -webkit-optimize-contrast;
    image-rendering: crisp-edges;
    image-rendering: pixelated;
  }}
  .info-tag {{
    font-size: 12px;
    color: #64748b;
    text-align: center;
    margin-top: 8px;
  }}
</style>
</head>
<body>
  <div class="toolbar">
    <h2>🖨️ Tamil Thermal Receipt Simulator (80mm / 576px)</h2>
    <a href="{raster_src}" download="tamil_receipt_576px.png" class="btn btn-primary">⬇️ Download Production PNG</a>
    <button onclick="location.reload()" class="btn btn-secondary">🔄 Refresh</button>
  </div>

  <div class="paper-container">
    <img src="{raster_src}" alt="Tamil Thermal Receipt Production Bitmap" />
  </div>
  <div class="info-tag">Exact 576px production bitmap passed to ESC/POS <code>bitImageRaster</code>.</div>
</body>
</html>"""
        return html

    # ═════════════════════════════════════════════════════════════════════════
    # HARDWARE VALIDATION MODE — TAMIL HARDWARE TEST
    # ═════════════════════════════════════════════════════════════════════════

    @app.route('/api/printer/hardware-test/raw-raster', methods=['POST'])
    def printer_hardware_test_raw_raster():
        """TEST 1 — RAW RASTER TEST

        Generates a small bitmap using the EXACT production TamilReceiptRasterRenderer
        and sends through ESC/POS bitImageRaster.
        Saves debug_output/client_tamil_hardware_test.png and .bin.
        """
        import datetime
        import json
        import os
        from services.tamil_text_render import TamilReceiptRasterRenderer
        from services.escpos_generator import EscposGenerator

        mgr = _manager()
        settings = mgr.load_settings()
        paper_w = int(settings.get('paper_width_mm') or 80)

        renderer = TamilReceiptRasterRenderer(settings)
        image = renderer.render_raw_raster_test_image(paper_width_mm=paper_w)

        _debug_dir()
        png_path = os.path.join(_debug_dir(), 'client_tamil_hardware_test.png')
        bin_path = os.path.join(_debug_dir(), 'client_tamil_hardware_test.bin')
        meta_path = os.path.join(_debug_dir(), 'client_tamil_hardware_test_meta.json')

        image.save(png_path, format='PNG')

        gen = EscposGenerator(settings)
        raw_bytes = gen.generate_image_raster_bytes(image)
        with open(bin_path, 'wb') as f:
            f.write(raw_bytes)

        svc = ThermalPrinterService(settings)
        transport = svc.get_active_transport()

        print_status = 'SENT'
        print_msg = f'Raster test accepted by {transport} (check the paper)'
        try:
            svc._send_raw(raw_bytes)
        except Exception as e:
            print_status = 'SEND_FAILED'
            print_msg = f'Print FAILED via {transport}: {e}'

        now_ts = datetime.datetime.now().isoformat()
        meta = {
            'test_type': 'raw_raster',
            'software_validation': 'PASS',
            'bitmap_width': image.width,
            'bitmap_height': image.height,
            'escpos_bytes_length': len(raw_bytes),
            'printer_transport': transport,
            'paper_width_mm': paper_w,
            'timestamp': now_ts,
            'print_attempt_status': print_status,
            'print_attempt_message': print_msg,
            'png_file': 'client_tamil_hardware_test.png',
            'bin_file': 'client_tamil_hardware_test.bin',
        }
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        return jsonify({
            'success': print_status == 'SENT',
            'software_validation': 'PASS',
            'physical_printer_validation': 'PENDING',
            'next_step': ('Inspect the paper and complete the operator checklist.'
                          if print_status == 'SENT' else
                          'Nothing reached the printer - fix the printer connection and run the test again.'),
            'test_type': 'raw_raster',
            'bitmap_width': image.width,
            'bitmap_height': image.height,
            'escpos_bytes_length': len(raw_bytes),
            'printer_transport': transport,
            'paper_width_mm': paper_w,
            'timestamp': now_ts,
            'print_status': print_status,
            'print_message': print_msg,
            'png_url': '/debug/client-hardware-test.png',
            'bin_url': '/debug/client-hardware-test.bin',
        })

    @app.route('/api/printer/hardware-test/full-bill', methods=['POST'])
    def printer_hardware_test_full_bill():
        """TEST 2 — FULL TAMIL BILL TEST

        Generates full production Tamil receipt bitmap using TamilReceiptRasterRenderer
        and sends through ESC/POS bitImageRaster.
        Saves debug_output/client_tamil_hardware_test.png and .bin.
        """
        import datetime
        import json
        import os
        from services.tamil_text_render import TamilReceiptRasterRenderer
        from services.escpos_generator import EscposGenerator

        mgr = _manager()
        settings = mgr.load_settings()
        paper_w = int(settings.get('paper_width_mm') or 80)

        test_ctx = {
            'language': 'tamil',
            'is_estimate': False,
            'is_normal': True,
            'is_oil_product': False,
            'shop': {
                'name_ta': 'ஸ்ரீ முருகன் ஆர்கானிக் ஸ்டோர்ஸ்',
                'name_en': 'Sri Murugan Organic Stores',
                'address_line1_ta': '123, காந்தி ரோடு, அண்ணா நகர்',
                'address_line2_ta': 'திருவண்ணாமலை, தமிழ்நாடு - 606601',
                'phone': '9876543210',
            },
            'bill': {
                'id': 'INV-2026-0042',
                'date': '24-09-2026',
                'time': '10:45:30 AM',
                'customer_name': 'செல்வகுமார் ராமச்சந்திரன் (Selvakumar R)',
                'customer_phone': '9841098410',
                'items': [
                    {
                        'tamil_name': 'குதிரைவாலி சிறுதானிய அரிசி பாரம்பரிய இயற்கை முறையில் விளைவிக்கப்பட்டது (Kuthiraivali)',
                        'qty': '2.500',
                        'amount': '325.00',
                    },
                    {
                        'tamil_name': 'நாட்டு சர்க்கரை (Country Sugar)',
                        'qty': '1',
                        'amount': '85.50',
                    },
                    {
                        'tamil_name': 'செக்கு நல்லெண்ணெய் 1 லிட்டர்',
                        'qty': '2',
                        'amount': '640.00',
                    },
                    {
                        'tamil_name': 'தூய மல்லி அரிசி 5 கிலோ',
                        'qty': '1',
                        'amount': '420.00',
                    },
                    {
                        'tamil_name': 'மரச்செக்கு தேங்காய் எண்ணெய் 500 மிலி',
                        'qty': '1',
                        'amount': '195.00',
                    },
                ],
                'total_items': 5,
                'total_qty': '7.500',
                'subtotal': 1665.50,
                'discount': 65.50,
                'total': 1600.00,
                'payment_method': 'UPI',
                'paid': 1600.00,
                'returned': 0.00,
                'due': 0.00,
                'loyalty': {
                    'stars': 160,
                },
            },
            'nutrition': {
                'protein': 12.5,
                'absorbable_protein': 9.2,
                'fat': 4.1,
                'carbs': 72.8,
                'fiber': 8.4,
                'ash': 1.6,
            },
        }

        renderer = TamilReceiptRasterRenderer(settings)
        image = renderer.render_receipt_image(test_ctx, paper_width_mm=paper_w)

        _debug_dir()
        png_path = os.path.join(_debug_dir(), 'client_tamil_hardware_test.png')
        bin_path = os.path.join(_debug_dir(), 'client_tamil_hardware_test.bin')
        meta_path = os.path.join(_debug_dir(), 'client_tamil_hardware_test_meta.json')

        image.save(png_path, format='PNG')

        gen = EscposGenerator(settings)
        raw_bytes = gen.generate_image_raster_bytes(image)
        with open(bin_path, 'wb') as f:
            f.write(raw_bytes)

        svc = ThermalPrinterService(settings)
        transport = svc.get_active_transport()

        print_status = 'SENT'
        print_msg = f'Full Tamil bill accepted by {transport} (check the paper)'
        try:
            svc._send_raw(raw_bytes)
        except Exception as e:
            print_status = 'SEND_FAILED'
            print_msg = f'Print FAILED via {transport}: {e}'

        now_ts = datetime.datetime.now().isoformat()
        meta = {
            'test_type': 'full_bill',
            'software_validation': 'PASS',
            'bitmap_width': image.width,
            'bitmap_height': image.height,
            'escpos_bytes_length': len(raw_bytes),
            'printer_transport': transport,
            'paper_width_mm': paper_w,
            'timestamp': now_ts,
            'print_attempt_status': print_status,
            'print_attempt_message': print_msg,
            'png_file': 'client_tamil_hardware_test.png',
            'bin_file': 'client_tamil_hardware_test.bin',
        }
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        return jsonify({
            'success': print_status == 'SENT',
            'software_validation': 'PASS',
            'physical_printer_validation': 'PENDING',
            'next_step': ('Inspect the paper and complete the operator checklist.'
                          if print_status == 'SENT' else
                          'Nothing reached the printer - fix the printer connection and run the test again.'),
            'test_type': 'full_bill',
            'bitmap_width': image.width,
            'bitmap_height': image.height,
            'escpos_bytes_length': len(raw_bytes),
            'printer_transport': transport,
            'paper_width_mm': paper_w,
            'timestamp': now_ts,
            'print_status': print_status,
            'print_message': print_msg,
            'png_url': '/debug/client-hardware-test.png',
            'bin_url': '/debug/client-hardware-test.bin',
        })

    @app.route('/api/printer/hardware-test/confirm', methods=['POST'])
    def printer_hardware_test_confirm():
        """TEST 4 — OPERATOR PHYSICAL PRINT CONFIRMATION

        Validates all 12 operator checklist items and writes
        debug_output/client_tamil_hardware_validation.json.
        """
        import datetime
        import json
        import os

        data = request.get_json(silent=True) or {}
        checklist = data.get('checklist') or {}
        operator_name = (data.get('operator_name') or 'Operator').strip()
        notes = (data.get('notes') or '').strip()

        required_keys = [
            'tamil_glyphs_readable',
            'tamil_vowels_consonants_correct',
            'no_missing_glyphs',
            'no_clipping_left',
            'no_clipping_right',
            'product_names_wrap',
            'qty_column_aligned',
            'amount_column_aligned',
            'currency_and_numbers_correct',
            'total_correct',
            'payment_section_correct',
            'footer_complete',
        ]

        missing_or_unchecked = [k for k in required_keys if not checklist.get(k)]
        if missing_or_unchecked:
            return jsonify({
                'success': False,
                'message': f'Please verify all 12 checklist items before confirming: {missing_or_unchecked}',
                'missing_keys': missing_or_unchecked,
            }), 400

        meta_path = os.path.join(_debug_dir(), 'client_tamil_hardware_test_meta.json')
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        if meta.get('print_attempt_status') != 'SENT':
            return jsonify({
                'success': False,
                'message': 'No hardware test has reached the printer yet. Run a hardware test that '
                           'prints successfully, inspect the paper, then confirm.',
                'physical_validation_status': 'PHYSICAL PRINTER VALIDATION: PENDING',
            }), 409
        current_transport = ThermalPrinterService(_manager().load_settings()).get_active_transport()
        if meta.get('printer_transport') != current_transport:
            return jsonify({
                'success': False,
                'message': f'The last test was printed on "{meta.get("printer_transport")}" but the '
                           f'configured printer is now "{current_transport}". Run the test again.',
                'physical_validation_status': 'PHYSICAL PRINTER VALIDATION: PENDING',
            }), 409
        now_ts = datetime.datetime.now().isoformat()
        validation_record = {
            'software_validation': 'PASS',
            'physical_printer_validation': 'PASS',
            'test_timestamp': meta.get('timestamp'),
            'test_type': meta.get('test_type'),
            'printer_transport': current_transport,
            'confirmed_at': now_ts,
            'operator_name': operator_name,
            'notes': notes,
            'checklist': checklist,
        }

        _debug_dir()
        val_path = os.path.join(_debug_dir(), 'client_tamil_hardware_validation.json')
        with open(val_path, 'w', encoding='utf-8') as f:
            json.dump(validation_record, f, indent=2, ensure_ascii=False)

        return jsonify({
            'success': True,
            'physical_validation_status': 'PHYSICAL PRINTER VALIDATION: PASS',
            'confirmed_at': now_ts,
            'operator_name': operator_name,
            'record': validation_record,
        })

    @app.route('/api/printer/hardware-test/status', methods=['GET'])
    def printer_hardware_test_status():
        """Return current status of hardware validation tests and confirmation."""
        import json
        import os

        meta_path = os.path.join(_debug_dir(), 'client_tamil_hardware_test_meta.json')
        val_path = os.path.join(_debug_dir(), 'client_tamil_hardware_validation.json')

        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
            except Exception:
                pass

        validation = {}
        if os.path.exists(val_path):
            try:
                with open(val_path, 'r', encoding='utf-8') as f:
                    validation = json.load(f)
            except Exception:
                pass

        current_transport = ThermalPrinterService(_manager().load_settings()).get_active_transport()
        physical = _physical_status(meta, validation, current_transport)
        if validation:
            validation = dict(validation)
            validation['physical_printer_validation'] = physical   # never report a stale PASS
        return jsonify({
            'success': True,
            'software_validation': 'PASS' if meta else 'NOT RUN',
            'physical_printer_validation': physical,
            'current_transport': current_transport,
            'last_test': meta,
            'validation': validation,
            'png_exists': os.path.exists(os.path.join(_debug_dir(), 'client_tamil_hardware_test.png')),
            'bin_exists': os.path.exists(os.path.join(_debug_dir(), 'client_tamil_hardware_test.bin')),
        })

    @app.route('/debug/client-hardware-test.png', methods=['GET'])
    def debug_client_hardware_test_png():
        import os
        from flask import send_file, abort
        p = os.path.abspath(os.path.join(_debug_dir(), 'client_tamil_hardware_test.png'))
        if not os.path.exists(p):
            return abort(404, 'Hardware test image not generated yet. Run a hardware test first.')
        return send_file(p, mimetype='image/png', download_name='client_tamil_hardware_test.png')

    @app.route('/debug/client-hardware-test.bin', methods=['GET'])
    def debug_client_hardware_test_bin():
        import os
        from flask import send_file, abort
        p = os.path.abspath(os.path.join(_debug_dir(), 'client_tamil_hardware_test.bin'))
        if not os.path.exists(p):
            return abort(404, 'Hardware test ESC/POS binary not generated yet. Run a hardware test first.')
        return send_file(p, mimetype='application/octet-stream', download_name='client_tamil_hardware_test.bin')


