"""Fixed receipt contexts for the English ESC/POS regression baseline.

tests/baseline/hashes.json was generated from the PRE-AUDIT code with these exact
contexts. test_matches_pre_audit_baseline() proves English output is unchanged.
"""
import json


def _row(bid, items, total, paid, due, cname, cphone, status, method):
    return {'id': bid, 'items': json.dumps(items, ensure_ascii=False), 'total': total, 'paid_amount': paid,
            'due_amount': due, 'customer_name': cname, 'customer_phone': cphone,
            'created_at': '28/09/2026 10:15:30', 'payment_status': status, 'payment_method': method,
            'cash': 0, 'balance': 0, 'bill_language': 'en'}


def _item(pid, en, ta, wg, ppk):
    amt = round(ppk * wg / 1000, 2)
    return {'product_id': pid, 'name': en, 'name_english': en, 'name_tamil': ta, 'tamil_name': ta,
            'weight_g': wg, 'weight_kg': wg / 1000, 'price_per_kg': ppk, 'total_price': amt, 'qty': wg / 1000,
            'nutrition_display': {'protein': 10, 'fat': 3, 'carbohydrates': 60, 'fiber': 6}}


ITEMS = [_item('p2', 'Red Foxtail Millet', 'செந்தினை', 1000, 90),
         _item('p5', 'Native Pearl Millet', 'நாட்டு கம்பு', 1500, 60),
         _item('p9', 'Barnyard Millet Rice Traditional Variety', 'குதிரைவாலி சிறுதானிய அரிசி பாரம்பரிய', 250, 220)]

CASES = {
    'paid_exact':  (_row('201', ITEMS, 395.0, 395.0, 0, 'Vishnu', '9962448809', 'PAID', 'cash'), None),
    'paid_change': (_row('202', ITEMS, 395.0, 395.0, 0, '', '', 'PAID', 'cash'), 500.0),
    'upi':         (_row('203', ITEMS[:1], 90.0, 90.0, 0, '', '', 'PAID', 'upi'), 90.0),
}


def build_all(app_module, languages=('english', 'tamil')):
    from services.thermal_printer import ThermalPrinterService
    svc = ThermalPrinterService({'mode': 'escpos', 'paper_width_mm': 80, 'chars_per_line': 48, 'auto_cut': True})
    out = {}
    for name, (row, override) in CASES.items():
        for lang in languages:
            for bt in ('normal', 'estimate'):
                ctx = app_module._build_print_context(row, lang, bt, paid_amount_override=override)
                ctx['language'] = lang
                out[f'{name}.{lang}.{bt}'] = svc.build_raw_receipt(ctx)[1]
    return out
