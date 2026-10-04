"""
Production-audit regression suite — DHANA DHANYA KADAI billing.

    python -m unittest discover -s tests -v

* Runs against a TEMPORARY COPY of the project (the real backend/billing.db is
  copied, never opened for writing).
* Needs no printer: ESC/POS bytes and Tamil raster bitmaps are generated and
  inspected in memory. Physical printing must still be validated by hand.
"""

import contextlib
import io
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV = None
A = None          # the imported app module (from the temp copy)


def setUpModule():
    global _ENV, A
    _ENV = tempfile.mkdtemp(prefix='billing_audit_')
    for part in ('backend', 'frontend', 'tools'):
        if not os.path.isdir(os.path.join(PROJECT, part)):
            continue
        shutil.copytree(os.path.join(PROJECT, part), os.path.join(_ENV, part),
                        ignore=shutil.ignore_patterns('db_backups', '__pycache__', 'logs', '.secret_key'))
    sys.path.insert(0, os.path.join(_ENV, 'backend'))
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()):
        import app as _app
        _app.init_db()
    A = _app
    con = sqlite3.connect(A.DB_PATH)
    # Deterministic test products (prices chosen to exercise rounding ties)
    con.executemany(
        "INSERT OR REPLACE INTO products (id,name_english,name_tamil,name_tanglish,name,price,price_per_kg,"
        "s_rate,p_rate,wholesale_rate,category,nutrients) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [('t_ragi', 'Ragi', 'கேழ்வரகு', 'kezhvaragu', 'Ragi', 90, 90, 90, 60, 75, 'Millets', '{}'),
         ('t_tie', 'Tie Test', 'சோதனை', 'sothanai', 'Tie Test', 50.5, 50.5, 50.5, 40, 45, 'Seeds', '{}'),
         ('t_long', 'Groundnut', 'வேர்க்கடலைவேர்க்கடலைவேர்க்கடலைவேர்க்கடலை', 'x', 'Groundnut',
          100, 100, 100, 80, 90, 'Groundnuts', '{}')])
    con.execute("UPDATE loyalty_settings SET loyalty_enabled=1, min_purchase_for_star=500, "
                "stars_for_reward=2, reward_discount=100 WHERE id=1")
    con.commit()
    con.close()


def tearDownModule():
    logging.disable(logging.NOTSET)
    shutil.rmtree(_ENV, ignore_errors=True)


def client(logged_in=True):
    c = A.app.test_client()
    if logged_in:
        with c.session_transaction() as s:
            s['logged_in'] = True
    return c


def db():
    con = sqlite3.connect(A.DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def bill(c, items, **kw):
    payload = {'items': items, 'paid_amount': kw.pop('paid_amount', 10 ** 6)}
    payload.update(kw)
    return c.post('/bill', json=payload)


RAGI_1KG = [{'product_id': 't_ragi', 'weight_g': 1000}]


class BillingCalculations(unittest.TestCase):
    def test_single_item(self):
        r = bill(client(), RAGI_1KG)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json['bill']['total'], 90.0)

    def test_multiple_and_decimal_quantities(self):
        r = bill(client(), [{'product_id': 't_ragi', 'weight_g': 250},
                            {'product_id': 't_ragi', 'weight_g': 1234.5}])
        self.assertEqual([i['total_price'] for i in r.json['bill']['items']], [22.5, 111.11])
        self.assertEqual(r.json['bill']['total'], 133.61)

    def test_rounding_tie_is_half_up(self):
        # 50.5 ₹/kg × 250 g = 12.625 → 12.63 (old backend saved 12.62, cart showed 12.63)
        r = bill(client(), [{'product_id': 't_tie', 'weight_g': 250}])
        self.assertEqual(r.json['bill']['total'], 12.63)

    def test_large_amount(self):
        r = bill(client(), [{'product_id': 't_ragi', 'weight_g': 500000}])
        self.assertEqual(r.json['bill']['total'], 45000.0)

    def test_frontend_backend_rounding_parity(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node not installed')
        with open(os.path.join(PROJECT, 'frontend', 'script.js'), encoding='utf-8') as fh:
            js = fh.read()
        start = js.index('function _decParts'); end = js.index('/** HTML-escape')
        cases = [(p / 2, w) for p in range(40, 400, 7) for w in range(5, 5000, 45)] + [(50.5, 250), (267.5, 10), (33.33, 333.3)]
        prog = js[start:end] + '\nconst C=' + json.dumps(cases) + ';console.log(JSON.stringify(C.map(([p,w])=>calcPrice(p,w))));'
        out = json.loads(subprocess.run([node, '-e', prog], capture_output=True, text=True, check=True).stdout)
        mism = [(p, w, o, A.line_amount(p, w)) for (p, w), o in zip(cases, out) if o != A.line_amount(p, w)]
        self.assertEqual(mism, [], f'{len(mism)} frontend/backend mismatches')


class PricingModes(unittest.TestCase):
    def test_modes_are_charged_as_displayed(self):
        c = client()
        self.assertEqual(bill(c, RAGI_1KG, price_mode='s_rate').json['bill']['total'], 90.0)
        self.assertEqual(bill(c, RAGI_1KG, price_mode='wholesale').json['bill']['total'], 75.0)
        self.assertEqual(bill(c, RAGI_1KG, price_mode='p_rate').json['bill']['total'], 60.0)

    def test_screen_price_mismatch_is_refused(self):
        r = bill(client(), [{'product_id': 't_ragi', 'weight_g': 1000, 'expected_price_per_kg': 75}],
                 price_mode='s_rate')
        self.assertEqual(r.status_code, 400)
        self.assertIn('changed', r.json['message'])

    def test_temp_price_override(self):
        r = bill(client(), [{'product_id': 't_ragi', 'weight_g': 1000, 'temp_price_per_kg': 80}])
        self.assertEqual(r.json['bill']['total'], 80.0)


class Payments(unittest.TestCase):
    PH = '9840011111'

    def test_full_payment(self):
        b = bill(client(), RAGI_1KG, paid_amount=90).json['bill']
        self.assertEqual((b['paid_amount'], b['due_amount'], b['payment_status']), (90, 0, 'PAID'))

    def test_partial_payment_requires_and_tracks_customer(self):
        c = client()
        self.assertEqual(bill(c, RAGI_1KG, paid_amount=50, payment_method='credit').status_code, 400)
        b = bill(c, RAGI_1KG, paid_amount=50, payment_method='credit', customer_phone=self.PH).json['bill']
        self.assertEqual((b['paid_amount'], b['due_amount'], b['payment_status']), (50, 40, 'CREDIT'))

    def test_overpayment_is_capped_change_returned(self):
        b = bill(client(), RAGI_1KG, paid_amount=150, cash=150).json['bill']
        self.assertEqual((b['paid_amount'], b['due_amount'], b['balance']), (90, 0, 60))

    def test_zero_payment_is_full_due(self):
        b = bill(client(), RAGI_1KG, paid_amount=0, payment_method='credit',
                 customer_phone='9840022222').json['bill']
        self.assertEqual((b['paid_amount'], b['due_amount']), (0, 90))

    def test_negative_or_nan_amounts_rejected(self):
        c = client()
        for kw in ({'paid_amount': -500}, {'cash': 'NaN'}, {'paid_amount': 'abc'}):
            self.assertEqual(bill(c, RAGI_1KG, **kw).status_code, 400, kw)

    def test_customer_due_matches_bill_records(self):
        c, ph = client(), '9840033333'
        bill(c, RAGI_1KG, paid_amount=0, payment_method='credit', customer_phone=ph)
        bill(c, RAGI_1KG, paid_amount=40, payment_method='credit', customer_phone=ph)
        self.assertEqual(c.post(f'/api/customer/{ph}/collect-payment', json={'amount': 100}).status_code, 200)
        con = db()
        cust_due = con.execute('SELECT total_due FROM customers WHERE phone=?', (ph,)).fetchone()[0]
        bills_due = con.execute('SELECT ROUND(SUM(due_amount),2) FROM bills WHERE customer_phone=?', (ph,)).fetchone()[0]
        paid_hist = con.execute('SELECT SUM(amount) FROM payment_history WHERE customer_id=?', (ph,)).fetchone()[0]
        con.close()
        self.assertEqual(cust_due, 40.0)
        self.assertEqual(bills_due, 40.0)
        self.assertEqual(paid_hist, 100.0)
        # cannot delete a customer who still owes money
        self.assertEqual(c.delete(f'/admin/customers/{ph}').status_code, 409)

    def test_phone_is_normalised(self):
        b = bill(client(), RAGI_1KG, customer_phone='+91 98400 44444').json['bill']
        self.assertEqual(b['customer_phone'], '919840044444')


class CashShortPayment(unittest.TestCase):
    """Cash mode: cash typed below the total must never be saved as PAID.
    Payloads match what the POS sends in Cash mode (paid_amount = total)."""
    BILL_500 = [{'product_id': 't_ragi', 'weight_g': 1000, 'temp_price_per_kg': 500}]

    def count(self):
        con = db(); n = con.execute('SELECT COUNT(*) FROM bills').fetchone()[0]; con.close(); return n

    def cash_bill(self, cash, **kw):
        kw.setdefault('payment_method', 'cash')
        return bill(client(), self.BILL_500, paid_amount=500, cash=cash, **kw)

    def test_500_bill_300_cash_is_refused(self):
        before = self.count()
        r = self.cash_bill(300)
        self.assertEqual(r.status_code, 400)
        self.assertIn('less than the bill total', r.json['message'])
        self.assertIn('Credit', r.json['message'])
        self.assertIn('200.00', r.json['message'])          # shortfall shown
        self.assertEqual(self.count(), before)               # nothing written

    def test_500_bill_300_cash_refused_even_with_customer(self):
        r = self.cash_bill(300, customer_phone='9840099901')
        self.assertEqual(r.status_code, 400)

    def test_500_bill_500_cash_is_paid(self):
        r = self.cash_bill(500)
        self.assertEqual(r.status_code, 201)
        b = r.json['bill']
        self.assertEqual((b['total'], b['paid_amount'], b['due_amount'], b['balance'], b['payment_status']),
                         (500, 500, 0, 0, 'PAID'))

    def test_500_bill_600_cash_is_paid_with_change(self):
        r = self.cash_bill(600)
        self.assertEqual(r.status_code, 201)
        b = r.json['bill']
        self.assertEqual((b['total'], b['paid_amount'], b['due_amount'], b['balance'], b['payment_status']),
                         (500, 500, 0, 100, 'PAID'))

    def test_no_cash_typed_is_unchanged_exact_payment(self):
        b = self.cash_bill(0).json['bill']
        self.assertEqual((b['paid_amount'], b['due_amount'], b['payment_status']), (500, 0, 'PAID'))

    def test_upi_and_card_unchanged(self):
        for method in ('upi', 'card'):
            r = self.cash_bill(300, payment_method=method)
            self.assertEqual(r.status_code, 201, method)
            self.assertEqual(r.json['bill']['payment_status'], 'PAID', method)

    def test_credit_records_the_300_as_part_payment(self):
        r = bill(client(), self.BILL_500, paid_amount=300, cash=300, payment_method='credit',
                 customer_phone='9840099902')
        b = r.json['bill']
        self.assertEqual((b['paid_amount'], b['due_amount'], b['payment_status']), (300, 200, 'CREDIT'))

    def test_frontend_guard_matches(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node not installed')
        with open(os.path.join(PROJECT, 'frontend', 'script.js'), encoding='utf-8') as fh:
            js = fh.read()
        start = js.index('function _cashShortfall')
        end = js.index('\n}\n', start) + 3
        cases = [('cash', 300, 500), ('cash', 500, 500), ('cash', 600, 500), ('cash', 0, 500),
                 ('cash', 499.99, 500), ('upi', 300, 500), ('card', 300, 500), ('credit', 300, 500)]
        prog = js[start:end] + '\nconst C=' + json.dumps(cases) + \
            ';console.log(JSON.stringify(C.map(([m,c,t])=>_cashShortfall(m,c,t))));'
        out = json.loads(subprocess.run([node, '-e', prog], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out, [200, 0, 0, 0, 0.01, 0, 0, 0])


class Validation(unittest.TestCase):
    def count(self):
        con = db(); n = con.execute('SELECT COUNT(*) FROM bills').fetchone()[0]; con.close(); return n

    def test_bad_payloads_write_nothing_and_never_500(self):
        c, before = client(), self.count()
        bad = [None, {}, {'items': []}, {'items': 'x'}, {'items': [{'product_id': 'NOPE', 'weight_g': 1}]},
               {'items': [{'product_id': 't_ragi', 'weight_g': -1000}]},
               {'items': [{'product_id': 't_ragi', 'weight_g': 'NaN'}]},
               {'items': [{'product_id': 't_ragi', 'weight_g': 'abc'}]},
               {'items': RAGI_1KG, 'customer_name': None, 'payment_method': None, 'paid_amount': 90},
               {'items': [{'product_id': 't_ragi', 'weight_g': 1000, 'temp_price_per_kg': -5}]},
               {'items': RAGI_1KG, 'price_mode': 'free'}, {'items': RAGI_1KG, 'customer_phone': '123'}]
        for p in bad:
            r = c.post('/bill', json=p)
            self.assertIn(r.status_code, (201, 400), p)
        r = c.post('/bill', data='{bad', content_type='application/json')
        self.assertEqual(r.status_code, 400)
        # the only valid one above is the null-name payload (normalised, saved)
        self.assertEqual(self.count() - before, 1)

    def test_no_database_lock_after_bad_request(self):
        # Original bug: customer_name=null crashed AFTER the stock UPDATE, the
        # connection leaked holding the write lock, and the NEXT bill failed
        # with "database is locked".
        c = client()
        r1 = c.post('/bill', json={'items': RAGI_1KG, 'customer_name': None, 'paid_amount': 90})
        self.assertLess(r1.status_code, 500)
        r2 = bill(c, RAGI_1KG)
        self.assertEqual(r2.status_code, 201)

    def test_failure_inside_transaction_rolls_back_everything(self):
        c, ph = client(), '9840088888'
        con = db()
        con.execute("UPDATE products SET stock=10 WHERE id='t_ragi'"); con.commit()
        before = (con.execute('SELECT COUNT(*) FROM bills').fetchone()[0],
                  con.execute("SELECT stock FROM products WHERE id='t_ragi'").fetchone()[0])
        con.close()
        orig = A.reserve_next_bill_no
        A.reserve_next_bill_no = lambda conn=None: (_ for _ in ()).throw(RuntimeError('simulated crash'))
        try:
            r = bill(c, RAGI_1KG, customer_phone=ph)
        finally:
            A.reserve_next_bill_no = orig
        self.assertEqual(r.status_code, 500)
        con = db()
        after = (con.execute('SELECT COUNT(*) FROM bills').fetchone()[0],
                 con.execute("SELECT stock FROM products WHERE id='t_ragi'").fetchone()[0])
        cust = con.execute('SELECT 1 FROM customers WHERE phone=?', (ph,)).fetchone()
        con.execute("UPDATE products SET stock=NULL WHERE id='t_ragi'"); con.commit(); con.close()
        self.assertEqual(before, after)          # no bill, stock untouched
        self.assertIsNone(cust)                  # customer/loyalty not committed either
        self.assertEqual(bill(c, RAGI_1KG).status_code, 201)   # and no lock left behind

    def test_admin_price_validation(self):
        c = client()
        r = c.post('/admin/products', json={'name_english': 'Z', 's_rate': 'nan', 'p_rate': 1, 'wholesale_rate': 1})
        self.assertEqual(r.status_code, 400)
        r = c.put('/admin/products/t_ragi', json={'price': -1})
        self.assertEqual(r.status_code, 400)
        r = c.put('/admin/products/t_ragi/stock', json={'stock': 'inf'})
        self.assertEqual(r.status_code, 400)


class BillNumberingAndIdempotency(unittest.TestCase):
    def test_sequential_and_unique_under_concurrency(self):
        ids, errors = [], []
        def worker():
            try:
                r = bill(client(), RAGI_1KG)
                ids.append(int(r.json['bill']['id']))
            except Exception as e:      # pragma: no cover
                errors.append(e)
        ts = [threading.Thread(target=worker) for _ in range(12)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(ids), 12)
        self.assertEqual(len(set(ids)), 12)
        self.assertEqual(sorted(ids), list(range(min(ids), min(ids) + 12)))

    def test_retry_with_same_ref_returns_same_bill(self):
        c = client()
        r1 = bill(c, RAGI_1KG, client_bill_ref='cart-abc-1')
        r2 = bill(c, RAGI_1KG, client_bill_ref='cart-abc-1')
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json['duplicate'])
        self.assertEqual(r1.json['bill']['id'], r2.json['bill']['id'])

    def test_concurrent_double_submit_creates_one_bill(self):
        res = []
        ts = [threading.Thread(target=lambda: res.append(bill(client(), RAGI_1KG, client_bill_ref='dbl-click-7')))
              for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        con = db(); n = con.execute("SELECT COUNT(*) FROM bills WHERE client_bill_ref='dbl-click-7'").fetchone()[0]; con.close()
        self.assertEqual(n, 1)
        self.assertEqual(len({r.json['bill']['id'] for r in res}), 1)

    def test_deleted_numbers_not_reused(self):
        c = client()
        last = int(bill(c, RAGI_1KG).json['bill']['id'])
        con = db(); con.execute('DELETE FROM bills WHERE id=?', (str(last),)); con.commit(); con.close()
        self.assertEqual(int(bill(c, RAGI_1KG).json['bill']['id']), last + 1)


class Loyalty(unittest.TestCase):
    def test_earn_redeem_and_failed_bill_gives_nothing(self):
        c, ph = client(), '9840055555'
        big = [{'product_id': 't_ragi', 'weight_g': 6000}]           # ₹540 ≥ 500
        bill(c, big, customer_phone=ph)
        # failed bill (bad item) must not add a star
        bill(c, big + [{'product_id': 'NOPE', 'weight_g': 1}], customer_phone=ph)
        con = db(); self.assertEqual(con.execute('SELECT stars FROM customers WHERE phone=?', (ph,)).fetchone()[0], 1); con.close()
        b = bill(c, big, customer_phone=ph, apply_loyalty_reward=True).json['bill']
        self.assertEqual((b['total'], b['loyalty_discount']), (440.0, 100.0))
        con = db(); row = con.execute('SELECT stars FROM customers WHERE phone=?', (ph,)).fetchone(); con.close()
        self.assertEqual(row[0], 0)
        ctx = A._build_print_context(A._fetch_bill_row(b['id']), 'english', 'normal')
        self.assertEqual((ctx['bill']['subtotal'], ctx['bill']['discount'], ctx['bill']['total']), (540.0, 100.0, 440.0))


class Ordering(unittest.TestCase):
    def test_bills_sorted_chronologically(self):
        con = db()
        con.execute("INSERT INTO bills(id,items,total,created_at) VALUES('X1','[]',1,'30/08/2025 10:00:00')")
        con.execute("INSERT INTO bills(id,items,total,created_at) VALUES('X2','[]',1,'01/09/2025 10:00:00')")
        con.commit(); con.close()
        ids = [b['id'] for b in client().get('/bills').json['bills']]
        self.assertLess(ids.index('X2'), ids.index('X1'))


class PrintContext(unittest.TestCase):
    def test_credit_bill_prints_saved_paid_and_due(self):
        c = client()
        b = bill(c, RAGI_1KG, paid_amount=0, payment_method='credit', customer_phone='9840066666').json['bill']
        ctx = A._build_print_context(A._fetch_bill_row(b['id']), 'english', 'normal', paid_amount_override=90)
        self.assertEqual((ctx['bill']['paid'], ctx['bill']['due'], ctx['bill']['returned']), (0.0, 90.0, 0.0))
        from services.receipt_formatter import format_receipt_lines
        text = '\n'.join(format_receipt_lines(dict(ctx, language='english')))
        self.assertIn('Due         : Rs.90.00', text)

    def test_cash_tendered_shows_change(self):
        b = bill(client(), RAGI_1KG, paid_amount=90, cash=100).json['bill']
        ctx = A._build_print_context(A._fetch_bill_row(b['id']), 'english', 'normal', paid_amount_override=100)
        self.assertEqual((ctx['bill']['paid'], ctx['bill']['returned']), (100.0, 10.0))


class EnglishEscpos(unittest.TestCase):
    def _bytes(self, ctx):
        from services.thermal_printer import ThermalPrinterService
        return ThermalPrinterService({'paper_width_mm': 80, 'auto_cut': True}).build_raw_receipt(ctx)

    def test_layout_and_commands(self):
        b = bill(client(), RAGI_1KG, paid_amount=90).json['bill']
        ctx = A._build_print_context(A._fetch_bill_row(b['id']), 'english', 'normal'); ctx['language'] = 'english'
        lines, raw, _ = self._bytes(ctx)
        self.assertTrue(raw.startswith(b'\x1bM\x00'))          # ESC M 0 → Font A
        self.assertIn(b'\x1bE\x01', raw)                       # ESC E 1
        self.assertTrue(raw.endswith(b'\x1dV\x00'))            # full cut
        self.assertTrue(all(len(l) <= 48 for l in lines))
        self.assertTrue(all(ord(ch) < 128 for l in lines for ch in l))
        self.assertFalse(any(l.startswith(('Due', 'Discount', 'Subtotal')) for l in lines))

    def test_matches_pre_audit_baseline(self):
        base = os.path.join(PROJECT, 'tests', 'baseline')
        if not os.path.isdir(base):
            self.skipTest('no baseline directory')
        with open(os.path.join(base, 'hashes.json')) as fh:
            exp = json.load(fh)
        import hashlib
        sys.path.insert(0, os.path.join(PROJECT, 'tests'))
        from gen_outputs import build_all
        got = build_all(A)
        for key, meta in exp.items():
            if 'english' in key:
                self.assertEqual(hashlib.sha256(got[key]).hexdigest(), meta['sha'], key)


class TamilRaster(unittest.TestCase):
    def ctx(self, items, **bill_kw):
        b = {'id': '42', 'date': '28/09/2026', 'time': '10:15:00 AM', 'customer_name': 'வாடிக்கையாளர்',
             'customer_phone': '9840000000', 'items': items, 'total_items': len(items), 'total_qty': 1,
             'subtotal': 100, 'total': 100, 'paid': 100, 'returned': 0}
        b.update(bill_kw)
        return {'language': 'tamil', 'is_normal': True, 'shop': {'name_ta': 'தன தானிய கடை'}, 'bill': b,
                'nutrition': {'protein': 10}}

    def test_width_and_dynamic_height(self):
        from services.tamil_text_render import TamilReceiptRasterRenderer
        r = TamilReceiptRasterRenderer({'paper_width_mm': 80})
        one = r.render_receipt_image(self.ctx([{'tamil_name': 'கேழ்வரகு', 'qty': 1, 'amount': 90}]))
        many = r.render_receipt_image(self.ctx([{'tamil_name': 'கேழ்வரகு', 'qty': 1, 'amount': 90}] * 15))
        self.assertEqual(one.width, 576)
        self.assertGreater(many.height, one.height)
        small = TamilReceiptRasterRenderer({'paper_width_mm': 58}).render_receipt_image(self.ctx([]))
        self.assertEqual(small.width, 384)

    def test_no_line_exceeds_name_column(self):
        from services.tamil_text_render import TamilReceiptRasterRenderer, ShapedFontRenderer, grapheme_clusters
        r = TamilReceiptRasterRenderer({})
        f = ShapedFontRenderer(r._resolve_font_path(True), 22)
        for name in ('வேர்க்கடலைவேர்க்கடலைவேர்க்கடலைவேர்க்கடலை', 'SUPERLONGPRODUCTNAMEWITHOUTSPACES',
                     'குதிரைவாலி சிறுதானிய அரிசி பாரம்பரிய இயற்கை', 'கேழ்வரகு - Ragi'):
            lines = r.wrap_text_pixel_width(name, 265, f)
            self.assertTrue(all(f.line_width(l) <= 265 for l in lines), name)
            self.assertEqual(''.join(lines).replace(' ', ''), name.replace(' ', ''))
            # no line starts with a combining mark (vowel sign / virama split from its letter)
            self.assertTrue(all(grapheme_clusters(l)[0] == grapheme_clusters(l)[0].strip() for l in lines))

    def test_tamil_escpos_is_raster(self):
        from services.thermal_printer import ThermalPrinterService
        _, raw, _ = ThermalPrinterService({'paper_width_mm': 80}).build_raw_receipt(
            self.ctx([{'tamil_name': 'முந்திரி ₹', 'qty': 1, 'amount': 90}]))
        self.assertIn(b'\x1dv0', raw)                          # GS v 0 bitImageRaster
        self.assertNotIn('முந்திரி'.encode('utf-8'), raw)      # never sent as Unicode text

    def test_preview_uses_production_renderer(self):
        b = bill(client(), RAGI_1KG).json['bill']
        r = client().get(f'/debug/tamil-raster?bill_id={b["id"]}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'image/png')
        with open(os.path.join(PROJECT, 'backend', 'printer_routes.py'), encoding='utf-8') as fh:
            src = fh.read()
        self.assertIn('TamilReceiptRasterRenderer(settings)', src)


class BackupRestore(unittest.TestCase):
    def test_backup_verify_and_restore_into_temp(self):
        c = client()
        bill(c, RAGI_1KG, paid_amount=0, payment_method='credit', customer_phone='9840077777')
        self.assertTrue(A._run_backup())
        files = sorted(f for f in os.listdir(A.BACKUP_DIR) if f.endswith('.db'))
        latest = os.path.join(A.BACKUP_DIR, files[-1])
        tmp = os.path.join(_ENV, 'restored_copy.db')
        shutil.copy(latest, tmp)
        def snap(p):
            con = sqlite3.connect(p)
            d = {t: con.execute(f'SELECT * FROM {t} ORDER BY 1').fetchall()
                 for t in ('products', 'customers', 'bills', 'payment_history', 'app_settings', 'loyalty_settings')}
            con.close(); return d
        self.assertEqual(snap(A.DB_PATH), snap(tmp))
        out = subprocess.run([sys.executable, os.path.join(_ENV, 'tools', 'restore_backup.py'), '--verify-all'],
                             capture_output=True, text=True)
        self.assertIn('OK', out.stdout)

    def test_corrupt_foreign_file_does_not_break_retention(self):
        junk = os.path.join(A.BACKUP_DIR, 'billing_29990101_000000.db')
        with open(junk, 'wb') as fh:
            fh.write(b'garbage' * 50)
        self.assertTrue(A._run_backup())
        out = subprocess.run([sys.executable, os.path.join(_ENV, 'tools', 'restore_backup.py'), '--verify-all'],
                             capture_output=True, text=True)
        self.assertIn('BAD', out.stdout)
        os.remove(junk)

    def test_missing_backup_dir_is_recreated(self):
        shutil.rmtree(A.BACKUP_DIR)
        self.assertTrue(A._run_backup())
        self.assertTrue(os.listdir(A.BACKUP_DIR))


class SecurityAndApi(unittest.TestCase):
    def test_protected_routes_require_login(self):
        c = client(logged_in=False)
        for method, path in (('GET', '/bills'), ('POST', '/bill'), ('GET', '/admin/download-db'),
                             ('GET', '/debug/tamil-raster?bill_id=1'),
                             ('POST', '/api/printer/hardware-test/full-bill'),
                             ('GET', '/api/customer/9840000000/payment-history')):
            r = c.open(path, method=method, json={}, headers={'Accept': 'application/json'})
            self.assertIn(r.status_code, (401, 302), (method, path))
        self.assertEqual(c.get('/health').status_code, 200)

    def test_image_upload_extension_whitelist(self):
        r = client().post('/admin/products/t_ragi/image', json={'image_data': 'aGk=', 'original_filename': 'x.html'})
        self.assertEqual(r.status_code, 400)

    def test_malformed_json_never_500(self):
        c = client()
        for path in ('/bill', '/admin/products', '/admin/customers', '/api/customer/9840000000/collect-payment',
                     '/api/thermal/print', '/api/printer/preview', '/admin/reset-bill-number'):
            r = c.post(path, data='{not json', content_type='application/json')
            self.assertLess(r.status_code, 500, path)


class Diagnostics(unittest.TestCase):
    def test_physical_validation_stays_pending_without_printed_test(self):
        c = client()
        st = c.get('/api/printer/hardware-test/status').json
        self.assertEqual(st['physical_printer_validation'], 'PENDING')
        all_ok = {k: True for k in ('tamil_glyphs_readable', 'tamil_vowels_consonants_correct', 'no_missing_glyphs',
                                    'no_clipping_left', 'no_clipping_right', 'product_names_wrap',
                                    'qty_column_aligned', 'amount_column_aligned', 'currency_and_numbers_correct',
                                    'total_correct', 'payment_section_correct', 'footer_complete')}
        # no printer in the test environment → test cannot reach paper
        t = c.post('/api/printer/hardware-test/full-bill').json
        self.assertEqual(t['software_validation'], 'PASS')
        self.assertEqual(t['physical_printer_validation'], 'PENDING')
        self.assertFalse(t['success'])
        r = c.post('/api/printer/hardware-test/confirm', json={'checklist': all_ok})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(c.get('/api/printer/hardware-test/status').json['physical_printer_validation'], 'PENDING')

    def test_print_failure_is_reported(self):
        b = bill(client(), RAGI_1KG).json['bill']
        r = client().post('/api/thermal/print', json={'bill_id': b['id'], 'language': 'english'})
        # test env has no printer selected → configuration error, never a silent fallback
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json['success'])
        self.assertEqual(r.json['stage'], 'config')
        self.assertIn('No thermal printer configured', r.json['message'])
        # the bill still exists — a failed print never deletes/rolls back a bill
        self.assertIsNotNone(A._fetch_bill_row(b['id']))


class Startup(unittest.TestCase):
    def test_init_db_is_idempotent(self):
        con = db(); before = con.execute('SELECT COUNT(*) FROM products').fetchone()[0]; con.close()
        with contextlib.redirect_stdout(io.StringIO()):
            A.init_db(); A.init_db()
        con = db(); after = con.execute('SELECT COUNT(*) FROM products').fetchone()[0]; con.close()
        self.assertEqual(before, after)

    def test_deleted_product_not_resurrected(self):
        # Original bug: with fewer than 100 products, every startup re-seeded
        # (INSERT OR IGNORE) and brought deleted catalogue items back.
        saved = A.DB_PATH + '.pre_resurrect'
        shutil.copy(A.DB_PATH, saved)
        try:
            con = db()
            con.execute("DELETE FROM products WHERE id NOT IN "
                        "(SELECT id FROM products ORDER BY id LIMIT 50)")
            con.commit()
            left = con.execute('SELECT COUNT(*) FROM products').fetchone()[0]
            con.close()
            with contextlib.redirect_stdout(io.StringIO()):
                A.init_db()
            con = db(); now = con.execute('SELECT COUNT(*) FROM products').fetchone()[0]; con.close()
            self.assertEqual(now, left)
        finally:
            shutil.copy(saved, A.DB_PATH)
            os.remove(saved)

    def test_missing_font_gives_clear_error(self):
        from services.tamil_text_render import TamilReceiptRasterRenderer
        r = TamilReceiptRasterRenderer({'unicode_font_path': '/nonexistent.ttf'})
        self.assertTrue(r._resolve_font_path(True).endswith('.ttf'))   # falls back to bundled font


class _FakeWin32Raw:
    """Stands in for escpos.printer.Win32Raw — records writes, fails on demand."""
    instances = []

    def __init__(self, printer_name, write_error=None, close_error=None):
        self.printer_name = printer_name
        self.write_error, self.close_error = write_error, close_error
        self.writes, self.closed = [], 0
        _FakeWin32Raw.instances.append(self)

    def _raw(self, data):
        if self.write_error:
            raise self.write_error
        self.writes.append(data)

    def close(self):
        self.closed += 1
        if self.close_error:
            raise self.close_error


class StrictPrinting(unittest.TestCase):
    """One configured print path: it prints, or it fails with the exact error.
    Never a fallback to another mode, another printer, or the Windows default."""

    def setUp(self):
        from services.printer_manager import PrinterManager
        self.mgr = PrinterManager(A.get_db)
        self._saved = self.mgr.load_settings()
        _FakeWin32Raw.instances = []

    def tearDown(self):
        self.mgr.save_settings(self._saved)

    def _store(self, **kw):
        """Write settings straight to the DB (bypasses the save-route validation,
        to simulate an old or hand-edited configuration)."""
        from services.printer_manager import DEFAULT_SETTINGS
        s = dict(DEFAULT_SETTINGS)
        s.update(kw)
        self.mgr.save_settings(s)

    def _print(self, bill_id=None):
        if bill_id is None:
            bill_id = bill(client(), RAGI_1KG).json['bill']['id']
        return bill_id, client().post('/api/thermal/print', json={'bill_id': bill_id, 'language': 'english'})

    def _fake_win32(self, **fail):
        from unittest import mock
        return mock.patch('escpos.printer.Win32Raw', lambda name: _FakeWin32Raw(name, **fail))

    # ── G1: mode / settings validation ────────────────────────────────────────
    def test_invalid_mode_is_config_error_and_never_prints(self):
        from unittest import mock
        from services.thermal_printer import ThermalPrinterService
        for mode in ('usb', '', 'Browser ', 'ESCPOS', None):
            self._store(mode=mode, printer_name='TM-T82')
            with mock.patch.object(ThermalPrinterService, '_send_raw') as send:
                _, r = self._print()
            self.assertEqual(r.status_code, 400, mode)
            self.assertEqual(r.json['stage'], 'config')
            self.assertIn('Invalid print mode', r.json['message'])
            self.assertNotIn('print_url', r.json)     # no browser path either
            send.assert_not_called()

    def test_settings_save_rejects_invalid_and_keeps_old(self):
        self._store(mode='escpos', printer_name='TM-T82')
        bad = [
            {'mode': 'usb'},
            {'paper_width_mm': 70},
            {'usb_vendor_id': '04b8', 'usb_product_id': ''},
            {'printer_name': ''},                                     # escpos with no printer
            {'usb_vendor_id': '04b8', 'usb_product_id': '0e15'},      # + printer_name → ambiguous
            {'usb_vendor_id': 'zz', 'usb_product_id': '0e15', 'printer_name': ''},
            {'mode': 'qz', 'qz_printer_name': ''},
        ]
        for change in bad:
            r = client().post('/api/printer/settings', json={'settings': change})
            self.assertEqual(r.status_code, 400, change)
            self.assertFalse(r.json['success'])
            self.assertTrue(r.json['message'])
            self.assertEqual(self.mgr.load_settings()['printer_name'], 'TM-T82', change)
            self.assertEqual(self.mgr.load_settings()['mode'], 'escpos', change)
        ok = client().post('/api/printer/settings', json={'settings': {'printer_name': 'TM-T88'}})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.mgr.load_settings()['printer_name'], 'TM-T88')

    # ── G2: no Windows default printer, no silent printer choice ──────────────
    def test_windows_default_printer_is_never_used(self):
        import types
        from unittest import mock
        from services.thermal_printer import ThermalPrinterService
        fake = types.SimpleNamespace(
            GetDefaultPrinter=mock.Mock(return_value='Office Laser'),
            EnumPrinters=mock.Mock(return_value=[(0, 0, 'Office Laser')]),
            PRINTER_ENUM_LOCAL=2, PRINTER_ENUM_CONNECTIONS=4)
        self._store(mode='escpos', printer_name='')
        with mock.patch.dict(sys.modules, {'win32print': fake}), mock.patch('sys.platform', 'win32'):
            client().get('/api/printer/settings')                    # used to auto-save the default
            self.assertEqual(self.mgr.load_settings()['printer_name'], '')
            _, r = self._print()
            with self.assertRaises(RuntimeError) as cm:
                ThermalPrinterService(self.mgr.load_settings())._send_raw(b'\x1b@')
        self.assertEqual(r.status_code, 400)
        self.assertIn('No thermal printer configured', r.json['message'])
        self.assertIn('No thermal printer configured', str(cm.exception))
        fake.GetDefaultPrinter.assert_not_called()

    def test_usb_and_windows_printer_together_is_an_error(self):
        from services.thermal_printer import ThermalPrinterService
        self._store(mode='escpos', printer_name='TM-T82', usb_vendor_id='04b8', usb_product_id='0e15')
        _, r = self._print()
        self.assertEqual(r.status_code, 400)
        self.assertIn('Both a USB printer', r.json['message'])
        with self.assertRaises(RuntimeError):
            ThermalPrinterService(self.mgr.load_settings())._send_raw(b'\x1b@')

    def test_qz_prints_only_to_its_own_printer(self):
        self._store(mode='qz', qz_printer_name='', printer_name='TM-T82')
        _, r = self._print()
        self.assertEqual(r.status_code, 400)                          # no fallback to printer_name
        self.assertIn('QZ printer name', r.json['message'])
        self._store(mode='qz', qz_printer_name='QZ-TM', printer_name='TM-T82')
        _, r = self._print()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json['printer_name'], 'QZ-TM')

    # ── G3: Windows job errors are reported, never swallowed ─────────────────
    def test_success_writes_once_and_closes_job(self):
        self._store(mode='escpos', printer_name='TM-T82')
        with self._fake_win32():
            _, r = self._print()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json['success'])
        (p,) = _FakeWin32Raw.instances
        self.assertEqual(p.printer_name, 'TM-T82')
        self.assertEqual(len(p.writes), 1)
        self.assertTrue(p.writes[0].startswith(b'\x1bM\x00'))
        self.assertEqual(p.closed, 1)

    def test_end_doc_failure_is_reported(self):
        self._store(mode='escpos', printer_name='TM-T82')
        with self._fake_win32(close_error=RuntimeError('EndDocPrinter failed: job rejected')):
            _, r = self._print()
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.json['success'])
        self.assertIn('EndDocPrinter failed: job rejected', r.json['message'])

    def test_write_error_is_not_masked_by_cleanup_error(self):
        self._store(mode='escpos', printer_name='TM-T82')
        with self._fake_win32(write_error=OSError('WritePrinter: The device is not connected.'),
                              close_error=RuntimeError('cleanup failed')):
            _, r = self._print()
        self.assertEqual(r.status_code, 500)
        self.assertIn('The device is not connected.', r.json['message'])
        self.assertNotIn('cleanup failed', r.json['message'])
        self.assertNotIn('print_url', r.json)

    def test_repeated_failures_never_retry_or_switch_mode(self):
        self._store(mode='escpos', printer_name='TM-T82')
        bill_id = bill(client(), RAGI_1KG).json['bill']['id']
        con = db(); bills_before = con.execute('SELECT COUNT(*) FROM bills').fetchone()[0]; con.close()
        with self._fake_win32(write_error=OSError('printer offline')):
            for _ in range(3):
                _, r = self._print(bill_id)
                self.assertEqual(r.status_code, 500)
                self.assertEqual(r.json['mode'], 'escpos')
                self.assertNotIn('print_url', r.json)
        self.assertEqual(len(_FakeWin32Raw.instances), 3)             # one attempt per click
        con = db(); bills_after = con.execute('SELECT COUNT(*) FROM bills').fetchone()[0]; con.close()
        self.assertEqual(bills_after, bills_before)                  # printing never creates bills
        self.assertEqual(self.mgr.load_settings()['mode'], 'escpos')

    # ── explicit modes still work, and only when configured ──────────────────
    def test_browser_and_preview_only_when_configured(self):
        from unittest import mock
        from services.thermal_printer import ThermalPrinterService
        with mock.patch.object(ThermalPrinterService, '_send_raw') as send:
            self._store(mode='browser')
            _, r = self._print()
            self.assertEqual((r.status_code, r.json['mode']), (200, 'browser'))
            self.assertIn('print_url', r.json)
            self._store(mode='preview')
            _, r = self._print()
            self.assertEqual((r.status_code, r.json['mode']), (200, 'preview'))
            self.assertNotIn('print_url', r.json)
        send.assert_not_called()


class NoHiddenBrowserCopy(unittest.TestCase):
    """G10: the Print button is the only print workflow; Ctrl+P prints nothing."""

    def _js(self):
        with open(os.path.join(PROJECT, 'frontend', 'script.js'), encoding='utf-8') as f:
            return f.read()

    def test_print_button_does_not_fill_hidden_print_area(self):
        js = self._js()
        start = js.index('async function handlePrint()')
        body = js[start:js.index('function printBill()', start)]
        self.assertNotIn('renderPrintAreaFromServer(', body)
        self.assertNotIn('window.print(', body)
        self.assertNotIn('renderPrintAreaFromServer(', js)            # hidden-copy filler removed entirely
        with open(os.path.join(PROJECT, 'frontend', 'index.html'), encoding='utf-8') as f:
            self.assertNotIn('id="print-area"', f.read())             # and its hidden target

    def test_ctrl_p_is_blocked_in_pos_window(self):
        js = self._js()
        self.assertRegex(js, r"\(e\.ctrlKey \|\| e\.metaKey\) && !e\.altKey && String\(e\.key\)\.toLowerCase\(\) === 'p'")
        self.assertNotIn('window.print(', js)                         # POS script never prints itself


# ── G4 / G5 / G6 / G8: printer availability, timeouts, readable errors ───────

class _PyWinError(Exception):
    """Stands in for pywintypes.error (winerror, funcname, strerror)."""
    def __init__(self, winerror, funcname, strerror):
        super().__init__(winerror, funcname, strerror)
        self.winerror = winerror


def _fake_win32print(status=0, attributes=0, open_error=None, get_error=None):
    import types
    from unittest import mock
    return types.SimpleNamespace(
        OpenPrinter=mock.Mock(side_effect=open_error, return_value='hPrinter'),
        GetPrinter=mock.Mock(side_effect=get_error,
                             return_value={'Status': status, 'Attributes': attributes}),
        ClosePrinter=mock.Mock(),
        GetDefaultPrinter=mock.Mock(return_value='Office Laser'),
    )


class _FakeUsb:
    """Stands in for escpos.printer.Usb — records calls, fails on demand."""
    instances = []

    def __init__(self, vendor, product, write_error=None, **kwargs):
        self.vendor, self.product, self.kwargs = vendor, product, kwargs
        self.write_error = write_error
        self.writes, self.closed = [], 0
        _FakeUsb.instances.append(self)

    def _raw(self, data):
        self.writes.append(data)
        if self.write_error:
            raise self.write_error

    def close(self):
        self.closed += 1


class _HangingWin32Raw(_FakeWin32Raw):
    """A Win32Raw whose write blocks until `release` is set (a stuck driver)."""
    release = None

    def _raw(self, data):
        _HangingWin32Raw.release.wait(10)
        self.writes.append(data)


READABLE_CODES = {'invalid_request', 'invalid_settings', 'bill_not_found', 'printer_error',
                  'printer_unavailable', 'printer_busy', 'timeout', 'usb_error',
                  'receipt_error', 'unexpected_error'}


class PrintFailureHandling(unittest.TestCase):
    """Every failure: exact readable JSON error, nothing sent twice, no fallback."""

    setUp = StrictPrinting.setUp
    _store = StrictPrinting._store
    _print = StrictPrinting._print
    _fake_win32 = StrictPrinting._fake_win32

    def tearDown(self):
        StrictPrinting.tearDown(self)
        _FakeUsb.instances = []

    def assertReadableFailure(self, r, code, status):
        self.assertEqual(r.status_code, status, r.get_data(as_text=True))
        self.assertTrue(r.is_json, 'response must be JSON, never an HTML error page')
        self.assertFalse(r.json['success'])
        self.assertEqual(r.json['error_code'], code)
        self.assertIn(r.json['error_code'], READABLE_CODES)
        msg = r.json['message']
        self.assertIsInstance(msg, str)
        self.assertTrue(msg.strip())
        self.assertNotIn('Traceback', msg)
        self.assertNotIn('<html', msg.lower())
        self.assertNotIn('print_url', r.json)                        # never a browser fallback
        return msg

    def _win32print(self, **kw):
        from unittest import mock
        import escpos.printer  # noqa: F401 — import before win32print is faked
        fake = _fake_win32print(**kw)
        return fake, mock.patch.dict(sys.modules, {'win32print': fake})

    def _fake_usb(self, **fail):
        from unittest import mock
        return mock.patch('escpos.printer.Usb', lambda v, p, **kw: _FakeUsb(v, p, **fail, **kw))

    def _assert_mode_unchanged(self, mode='escpos'):
        self.assertEqual(self.mgr.load_settings()['mode'], mode)

    # ── G4: offline / unavailable Windows printer ─────────────────────────────
    def test_offline_windows_printer_is_refused_before_sending(self):
        self._store(mode='escpos', printer_name='TM-T82')
        cases = [(0x80, 0, 'is OFFLINE'), (0x1000, 0, 'is NOT AVAILABLE'),
                 (0x1, 0, 'is PAUSED in Windows'), (0, 0x400, 'Use Printer Offline')]
        for status, attributes, text in cases:
            fake, patched = self._win32print(status=status, attributes=attributes)
            with patched, self._fake_win32():
                _, r = self._print()
            msg = self.assertReadableFailure(r, 'printer_unavailable', 503)
            self.assertIn(text, msg)
            self.assertIn("'TM-T82'", msg)
            self.assertTrue(r.json['bill_saved'])
            fake.OpenPrinter.assert_called_once_with('TM-T82')
            fake.GetDefaultPrinter.assert_not_called()                # never another printer
        self.assertEqual(_FakeWin32Raw.instances, [])                  # nothing was ever sent
        self._assert_mode_unchanged()

    def test_windows_printer_not_installed(self):
        self._store(mode='escpos', printer_name='TM-T82')
        err = _PyWinError(1801, 'OpenPrinter', 'The printer name is invalid.')
        fake, patched = self._win32print(open_error=err)
        with patched, self._fake_win32():
            _, r = self._print()
        msg = self.assertReadableFailure(r, 'printer_unavailable', 503)
        self.assertIn('not installed', msg)
        self.assertEqual(_FakeWin32Raw.instances, [])
        fake.GetDefaultPrinter.assert_not_called()

    def test_unknown_printer_state_is_not_reported_offline(self):
        self._store(mode='escpos', printer_name='TM-T82')
        unknown = [
            dict(get_error=_PyWinError(5, 'GetPrinter', 'Access is denied.')),
            dict(open_error=_PyWinError(5, 'OpenPrinter', 'Access is denied.')),
            dict(status=0x10 | 0x2),       # paper-out / error flags: often stale → not blocking
            dict(status=0),
        ]
        for kw in unknown:
            _FakeWin32Raw.instances = []
            _, patched = self._win32print(**kw)
            with patched, self._fake_win32():
                _, r = self._print()
            self.assertEqual(r.status_code, 200, kw)
            self.assertTrue(r.json['success'])
            (p,) = _FakeWin32Raw.instances                            # printed exactly once
            self.assertEqual(p.printer_name, 'TM-T82')
            self.assertEqual(len(p.writes), 1)

    # ── G5: USB timeout ───────────────────────────────────────────────────────
    def test_usb_timeout_is_reported_without_retry(self):
        import usb.core
        from services import thermal_printer as tp
        self._store(mode='escpos', printer_name='', usb_vendor_id='04b8', usb_product_id='0e15')
        with self._fake_usb(write_error=usb.core.USBTimeoutError('Operation timed out', -7, 110)):
            _, r = self._print()
        msg = self.assertReadableFailure(r, 'timeout', 504)
        self.assertIn('did not accept the receipt within 12 seconds', msg)
        self.assertIn('Operation timed out', msg)                     # original error kept
        self.assertIn('BEFORE printing again', msg)
        (p,) = _FakeUsb.instances                                     # one attempt, no retry
        self.assertEqual(p.kwargs.get('timeout'), tp.USB_WRITE_TIMEOUT_MS)
        self.assertEqual(len(p.writes), 1)
        self.assertEqual(p.closed, 1)                                 # device released
        self._assert_mode_unchanged()

    def test_hung_send_hits_deadline_and_never_prints_twice(self):
        import time
        from unittest import mock
        from services import thermal_printer as tp
        self._store(mode='escpos', printer_name='TM-T82')
        _HangingWin32Raw.release = threading.Event()
        try:
            with mock.patch.object(tp, 'PRINT_DEADLINE_S', 0.3), \
                 mock.patch.object(tp, 'PRINT_LOCK_WAIT_S', 0.05), \
                 mock.patch('escpos.printer.Win32Raw', lambda name: _HangingWin32Raw(name)):
                t0 = time.monotonic()
                _, r1 = self._print()
                self.assertLess(time.monotonic() - t0, 5)             # did not hang
                msg = self.assertReadableFailure(r1, 'timeout', 504)
                self.assertIn('did not finish printing within 0.3 seconds', msg)
                self.assertIn('BEFORE printing again', msg)

                _, r2 = self._print()                                 # pressed again while stuck
                self.assertReadableFailure(r2, 'printer_busy', 409)
                self.assertEqual(len(_FakeWin32Raw.instances), 1)     # second job NOT sent

                _HangingWin32Raw.release.set()                        # driver finally returns
                self.assertTrue(tp._send_lock.acquire(timeout=5))
                tp._send_lock.release()
                _, r3 = self._print()
                self.assertEqual(r3.status_code, 200)
        finally:
            _HangingWin32Raw.release.set()
        self.assertEqual(sum(len(p.writes) for p in _FakeWin32Raw.instances), 2)   # stuck one + r3
        self._assert_mode_unchanged()

    # ── G6: every failure path returns readable JSON ─────────────────────────
    def test_printer_error_keeps_exact_message(self):
        self._store(mode='escpos', printer_name='TM-T82')
        with self._fake_win32(write_error=OSError('WritePrinter: The device is not connected.')):
            _, r = self._print()
        msg = self.assertReadableFailure(r, 'printer_error', 500)
        self.assertEqual(msg, 'WritePrinter: The device is not connected.')
        self.assertTrue(r.json['bill_saved'])

    def test_invalid_settings_error(self):
        self._store(mode='escpos', printer_name='')
        _, r = self._print()
        msg = self.assertReadableFailure(r, 'invalid_settings', 400)
        self.assertIn('No thermal printer configured', msg)

    def test_usb_failure_keeps_original_error(self):
        from escpos.exceptions import DeviceNotFoundError
        self._store(mode='escpos', printer_name='', usb_vendor_id='04b8', usb_product_id='0e15')
        with self._fake_usb(write_error=DeviceNotFoundError('Unable to open USB printer on (1208, 3605)')):
            _, r = self._print()
        msg = self.assertReadableFailure(r, 'usb_error', 500)
        self.assertIn('USB printer 04b8:0e15 failed', msg)
        self.assertIn('Unable to open USB printer on (1208, 3605)', msg)
        self.assertEqual(len(_FakeUsb.instances), 1)

    def test_bad_requests_are_readable(self):
        self._store(mode='escpos', printer_name='TM-T82')
        bill_id = bill(client(), RAGI_1KG).json['bill']['id']
        c = client()
        r = c.post('/api/thermal/print', json={'bill_id': bill_id, 'paid_amount': 'abc'})
        self.assertIn('Invalid paid amount', self.assertReadableFailure(r, 'invalid_request', 400))
        r = c.post('/api/thermal/print', json=[1, 2])
        self.assertReadableFailure(r, 'invalid_request', 400)
        r = c.post('/api/thermal/print', json={})
        self.assertReadableFailure(r, 'invalid_request', 400)
        r = c.post('/api/thermal/print', json={'bill_id': '99999999'})
        self.assertIn('not found', self.assertReadableFailure(r, 'bill_not_found', 404))

    # ── G8: unexpected exceptions — logged in full, shown short ──────────────
    def test_unexpected_exception_is_logged_but_not_shown(self):
        from unittest import mock
        from services.thermal_printer import ThermalPrinterService
        self._store(mode='escpos', printer_name='TM-T82')
        secret = KeyError(r'C:\Users\shop\AppData\secret_module.py line 42')
        bill_id = bill(client(), RAGI_1KG).json['bill']['id']
        logging.disable(logging.NOTSET)
        try:
            with mock.patch.object(ThermalPrinterService, 'print_receipt', side_effect=secret), \
                 self.assertLogs('billing.printer', 'ERROR') as logs:
                _, r = self._print(bill_id)
        finally:
            logging.disable(logging.CRITICAL)
        msg = self.assertReadableFailure(r, 'unexpected_error', 500)
        self.assertNotIn('secret_module', msg)
        self.assertNotIn('AppData', msg)
        self.assertNotIn('KeyError', msg)
        self.assertIn(r.json['reference'], msg)
        self.assertIn('Nothing was printed', msg)
        log_text = '\n'.join(logs.output)
        self.assertIn(r.json['reference'], log_text)
        self.assertIn('secret_module.py', log_text)                   # full detail in the log
        self.assertIsNotNone(A._fetch_bill_row(bill_id))             # bill still saved

    def test_printer_test_endpoint_errors_are_readable(self):
        from unittest import mock
        from services.thermal_printer import ThermalPrinterService
        self._store(mode='escpos', printer_name='TM-T82')
        with self._fake_win32(write_error=OSError('The printer is out of paper.')):
            r = client().post('/api/printer/test', json={'language': 'english'})
        self.assertEqual(self.assertReadableFailure(r, 'printer_error', 500), 'The printer is out of paper.')
        with mock.patch.object(ThermalPrinterService, 'print_test_receipt', side_effect=TypeError('boom')):
            r = client().post('/api/printer/test', json={'language': 'english'})
        self.assertNotIn('boom', self.assertReadableFailure(r, 'unexpected_error', 500))

    def test_diagnostic_ruler_success_is_not_reported_as_failure(self):
        # It used to call .get() on _send_raw()'s None → "failed" after printing.
        self._store(mode='escpos', printer_name='TM-T82')
        with self._fake_win32():
            r = client().get('/api/printer/diagnostic-ruler')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json['success'])
        (p,) = _FakeWin32Raw.instances
        self.assertEqual(len(p.writes), 1)

    def test_successful_escpos_print_is_exactly_one_attempt(self):
        self._store(mode='escpos', printer_name='TM-T82')
        with self._fake_win32():
            _, r = self._print()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json['mode'], 'escpos')
        (p,) = _FakeWin32Raw.instances
        self.assertEqual((len(p.writes), p.closed), (1, 1))


class SearchRanking(unittest.TestCase):
    """POS search: the best match is listed first so Enter adds the right product.
    Real search/render functions from script.js run in Node
    (tests/js/search_rank_harness.js) on fixture data and on this DB's /products."""

    @classmethod
    def setUpClass(cls):
        node = shutil.which('node')
        if not node:
            raise unittest.SkipTest('node is not installed')
        products = os.path.join(_ENV, 'products_for_search.json')
        with open(products, 'w', encoding='utf-8') as fh:
            json.dump(client().get('/products').json, fh, ensure_ascii=False)
        out = subprocess.run([node, os.path.join(PROJECT, 'tests', 'js', 'search_rank_harness.js'), products],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        cls.results = json.loads(out.stdout)
        if 'harness_error' in cls.results:
            raise AssertionError(cls.results['harness_error'])

    def q(self, query, data='fixture'):
        return self.results[data][query]

    def test_ragi_ranks_finger_millet_above_amaranth(self):
        for query in ('ragi', 'Ragi'):
            r = self.q(query)
            self.assertEqual(r['order'][:2], ['p14', 'p220'])        # S.NO order alone puts p220 first
            self.assertEqual(r['first_rendered_row'], 'p14')

    def test_ragi_on_real_product_data(self):
        r = self.results['real']['ragi']
        if not {'p14', 'p220'} <= set(r['matched']):
            self.skipTest('shop products P14/P220 not in this database')
        self.assertLess(r['order'].index('p14'), r['order'].index('p220'))
        self.assertLess(r['ranks']['p14'], r['ranks']['p220'])
        # The test DB also seeds t_ragi named exactly "Ragi" — an exact name, so it may lead.
        self.assertEqual(r['first_rendered_row'], r['order'][0])
        self.assertIn(r['order'][0], ('p14', 't_ragi'))

    def test_exact_code_first(self):
        for query in ('P52', 'p52'):
            r = self.q(query)
            self.assertEqual(r['order'][0], 'p52')                   # before p520 (S.NO 1)
            self.assertEqual(r['ranks']['p52'], 0)
            self.assertIn('p520', r['order'])                         # partial code match still listed

    def test_exact_english_and_tamil_names(self):
        for query in ('finger millet', 'Finger Millet', 'கேழ்வரகு'):
            self.assertEqual(self.q(query)['order'][0], 'p14')
            self.assertEqual(self.q(query)['ranks']['p14'], 1)
        self.assertEqual(self.q('pearl millet')['order'][0], 'p15')  # exact beats "Native Pearl Millet"
        self.assertEqual(self.q('தினை')['order'][0], 'p1')

    def test_exact_tanglish_and_alias(self):
        self.assertEqual(self.q('keezhvaraku')['order'][0], 'p14')
        self.assertEqual(self.q('kampu')['order'][:2], ['p15', 'p5'])   # exact Tanglish before "naattukampu"
        self.assertEqual(self.q('thinai')['order'][0], 'p1')            # exact Tanglish before "sivappu thinai"
        self.assertEqual(self.q('rajgira')['order'], ['p220'])          # registered alias "(rajgira)"

    def test_partial_matches_still_listed(self):
        self.assertEqual(set(self.q('thinai')['order']), {'p1', 'p2', 'p9'})
        self.assertEqual(set(self.q('millet')['order']), {'p1', 'p2', 'p5', 'p9', 'p14', 'p15'})
        self.assertEqual(self.q('urad')['order'], ['p52'])
        self.assertEqual(self.q('kambu')['order'], ['p5', 'p15'])       # synonym rule, S.NO order
        self.assertEqual(self.q('zzzz')['order'], [])

    def test_ranking_never_changes_which_products_match(self):
        for data in ('fixture', 'real'):
            for query, r in self.results[data].items():
                self.assertEqual(sorted(r['order']), sorted(r['matched']), (data, query))

    def test_enter_adds_top_ranked_row(self):
        # Enter in the search box adds the highlighted row, else the first row —
        # which the harness shows is the top-ranked product.
        with open(os.path.join(PROJECT, 'frontend', 'script.js'), encoding='utf-8') as fh:
            js = fh.read()
        start = js.index("if (document.activeElement?.id === 'productSearch') {")
        branch = js[start:js.index("if (e.altKey && e.key.toLowerCase() === 's')", start)]
        self.assertIn("document.querySelector('#products-grid .pcard.kb-focus')", branch)
        self.assertIn("highlighted || document.querySelector('#products-grid .pcard')", branch)
        self.assertIn('_highlightCard(0)', branch)                    # typing highlights the first row
        for r in self.results['fixture'].values():
            self.assertEqual(r['first_rendered_row'], r['order'][0] if r['order'] else None)


class PrintFlowFrontend(unittest.TestCase):
    """G9 / G10: the real POS print functions from script.js, run in Node with a
    fake server (tests/js/print_flow_harness.js)."""

    @classmethod
    def setUpClass(cls):
        node = shutil.which('node')
        if not node:
            raise unittest.SkipTest('node is not installed')
        out = subprocess.run([node, os.path.join(PROJECT, 'tests', 'js', 'print_flow_harness.js')],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise AssertionError(out.stderr)
        cls.results = json.loads(out.stdout)

    def scenario(self, name):
        r = self.results[name]
        self.assertNotIn('harness_error', r, r.get('harness_error'))
        return r

    def assertNoBrowserOrPdf(self, r):
        self.assertEqual(r['window_print'], 0)
        self.assertEqual(r['window_open'], [])
        self.assertEqual(r['preview_modal'], [])
        self.assertEqual(r['other_fetches'], [])                     # no /print-bill, no PDF route
        self.assertEqual(r['settings_writes'], 0)                    # mode never changed

    def test_escpos_success_prints_once_without_browser(self):
        r = self.scenario('escpos_success')
        self.assertEqual(r['print_requests'], 1)
        self.assertEqual(r['after_success'], 1)
        self.assertIsNone(r['dialog'])
        self.assertNoBrowserOrPdf(r)
        self.assertFalse(r['is_printing'])

    def test_escpos_failure_shows_exact_error_and_stops(self):
        r = self.scenario('escpos_failure')
        self.assertEqual(r['print_requests'], 1)                     # no automatic retry
        self.assertEqual(r['after_success'], 0)
        self.assertEqual(r['dialog']['message'], 'WritePrinter: The device is not connected.')
        self.assertIn('is saved, but it did NOT print', r['dialog']['title'])
        self.assertNoBrowserOrPdf(r)
        self.assertFalse(r['is_printing'])

    def test_unreadable_server_responses_become_readable(self):
        r = self.scenario('html_error_page')
        self.assertIn('unreadable response (HTTP 500)', r['dialog']['message'])
        self.assertNotIn('<', r['dialog']['message'])
        self.assertNoBrowserOrPdf(r)
        r = self.scenario('session_expired')
        self.assertIn('login session has expired', r['dialog']['message'])
        r = self.scenario('unexpected_server_error')
        self.assertIn('reference ABCD1234', r['dialog']['message'])
        self.assertIn('is saved, but it did NOT print', r['dialog']['title'])

    def test_print_timeout_stops_waiting_and_ignores_late_result(self):
        r = self.scenario('print_timeout_then_late_success')
        at = r['at_timeout']
        self.assertIn('did not finish within', at['dialog']['message'])
        self.assertIn('BEFORE pressing Print again', at['dialog']['message'])
        self.assertIn('is saved', at['dialog']['title'])
        self.assertFalse(at['is_printing'])                          # button usable again
        # the late "success" changed nothing: no reset, no second request, same dialog
        self.assertEqual(r['after_success'], 0)
        self.assertEqual(r['print_requests'], 1)
        self.assertEqual(r['dialog'], at['dialog'])
        self.assertNoBrowserOrPdf(r)

    def test_late_response_does_not_touch_a_newer_print(self):
        r = self.scenario('late_response_does_not_touch_newer_print')
        self.assertIn('nothing was printed', r['after_first']['dialog']['title'])
        self.assertFalse(r['after_first']['is_printing'])
        self.assertEqual(r['print_requests'], 1)                     # only the NEW attempt printed
        self.assertTrue(r['is_printing'])                            # new attempt not clobbered
        self.assertNoBrowserOrPdf(r)

    def test_save_timeout_never_prints(self):
        r = self.scenario('save_timeout_never_prints')
        self.assertEqual(r['print_requests'], 0)
        self.assertIn('Saving the bill did not finish', r['dialog']['message'])
        self.assertFalse(r['is_printing'])

    def test_reprint_has_its_own_time_limit(self):
        r = self.scenario('reprint_has_own_timeout')
        self.assertFalse(r['returned'])
        self.assertEqual(r['print_requests'], 1)
        self.assertIn('Bill #77', r['dialog']['title'])

    def test_browser_mode_only_when_explicitly_configured(self):
        r = self.scenario('browser_mode_explicit')
        self.assertEqual(len(r['window_open']), 1)
        self.assertTrue(r['window_open'][0].startswith('/print-bill/101?'))
        self.assertEqual(r['window_print'], 0)                       # POS window never prints itself
        self.assertEqual(r['pending_browser_bill'], '101')
        self.assertIsNone(r['dialog'])

    def test_preview_is_preview_only(self):
        r = self.scenario('preview_mode')
        self.assertEqual(len(r['preview_modal']), 1)
        self.assertEqual(r['window_print'], 0)
        self.assertEqual(r['window_open'], [])
        self.assertEqual(r['after_success'], 0)                      # not treated as printed

    def test_ctrl_p_does_not_print(self):
        r = self.scenario('ctrl_p_blocked')
        self.assertEqual(r['prevented'], 1)
        self.assertEqual(r['window_print'], 0)
        self.assertEqual(r['print_requests'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
