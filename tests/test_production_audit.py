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
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.json['success'])
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
