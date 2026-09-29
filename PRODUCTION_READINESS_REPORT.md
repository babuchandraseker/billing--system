# BILLING SYSTEM PRODUCTION AUDIT

**System:** Dhana Dhanya Kadai billing POS (Flask + SQLite + PyWebView). Printing uses ESC/POS; Tamil is printed as a HarfBuzz raster image.
**Audit date:** 28 Sep 2026.
**Audit environment:** Linux sandbox (Python 3.12, Node 22). There was no Windows machine, no WebView2 and no physical printer.
**Method:** Read-only audit first. Every defect was reproduced against a copy of the real `billing.db`, then fixed, then covered by an automated test. Each test was run against both the original and the fixed code.

---

## 1. Overall Status

**NOT READY**

**Why:** No known software defect remains unfixed. All 12 confirmed FAILs are fixed, and the 44-test suite passes on the fixed code (26 of those tests fail on the original code). Four deployment gates are still open. The first one is a verification gap on the most critical code path.

| Gate | Why it blocks | How to clear |
|---|---|---|
| G1 | The changed POS JavaScript (bill save, print retry, confirmation dialogs, shortcuts) has only been checked by `node --check` and by running the extracted rounding code in Node. It has never run inside WebView2. | Run §28 steps 5–9 on the store PC. |
| G2 | The login password is `1` (verified: the stored hash is `sha256("1")`). | Change it in Admin before go-live. |
| G3 | The shipped `billing.db` contains test bills 137–139 and test customers. | Clear them with Admin → Clear Analytics → All + delete bills. This now takes a backup automatically first. |
| G4 | **PHYSICAL PRINTER VALIDATION: PENDING.** Nothing in this audit was printed on paper. | Run §28 steps 10–13 on the client's Epson printer. |

Once G1–G4 are done, the status becomes **READY WITH WARNINGS** (the warnings are listed in §23).

---

## 2. Architecture

```
PyWebView window (EdgeChromium / WebView2)        desktop_app.py
   │  http://127.0.0.1:5000  (session cookie)
Frontend  frontend/index.html + script.js (6.7k lines) + tamil-keyboard.js + tamil-transliterate.js + qz-tray-print.js
   │  fetch() JSON
Flask     backend/app.py (≈5k lines, ~60 routes) + backend/printer_routes.py (22 routes)
   │
Services  nutrition_engine.py · services/receipt_formatter.py (English 48-col text)
          services/tamil_text_render.py (HarfBuzz+FreeType → PIL bitmap)
          services/escpos_generator.py (python-escpos Dummy → bytes)
          services/thermal_printer.py (Win32Raw / USB / default printer)
          services/printer_manager.py (settings in app_settings)
   │
SQLite    backend/billing.db — products, bills (items as JSON), customers, payment_history,
          packages, loyalty_settings, app_settings
   │
Printer / backup    Win32Raw spooler · pyusb · QZ Tray (browser) · browser/PDF fallback
                    db_backups/ (hourly, SQLite online-backup API)
```

**Entry points:** `run.bat` → `pythonw desktop_app.py`. `app.py __main__` is a dev-only server. `desktop_app.spec` is the PyInstaller build and `verify_build.py` checks the build.

**Dead or duplicate code found:**
- `frontend/script.js.bak` (removed).
- `debug_output/*` stale artifacts (removed).
- `_LEGACY_PRODUCT_NUTRITION_DB` in `app.py`: marked legacy, kept.
- `Open_Catalog.bat`: deprecated redirect.
- The line-based raster fallback in `EscposGenerator._generate_raster_receipt_bytes` (the `ctx is None` branch) is **unreachable** from production, because every caller passes `ctx`. It is the only code that uses "global font shrinking"; left in place and not used.

**Tamil rendering implementations:**
- **Printing:** there is exactly one, `TamilReceiptRasterRenderer`. It is used by production printing, QZ, `/debug/tamil-raster` and the hardware tests.
- **Browser HTML fallback:** uses a separate CSS layout (`format_tamil_html`).
- **wcwidth:** still used by `receipt_formatter` for the Tamil *text* preview (`/api/printer/preview`). That is display-only and never sent to the printer.

---

## 3. Frontend

**PASS**
- The cart, product search and customer lookup flows work as designed (code trace).
- `isPrinting` guards concurrent F3 presses.

**FAIL → FIXED**

| # | What failed | Where | Why | Change | Verified by |
|---|---|---|---|---|---|
| F1 | Wholesale / P.Rate price shown, but a different price charged | `script.js` payload vs `app.py save_bill` | The payload had no price mode, so the backend always used `price_per_kg` | Payload now includes `price_mode` and a per-item `expected_price_per_kg` | `PricingModes.*` |
| F2 | Print retry saved a duplicate bill | `handlePrint` → `_saveBillCore` | "Every Print = one DB record", and the cart is kept after a failed print | Cart-state signature, `client_bill_ref`, an in-flight lock, reuse of the saved bill, and a confirm prompt if the cart changed after saving | `BillNumberingAndIdempotency.*` (server side). The UI side is gate G1. |
| F4 | A credit bill printed as fully paid | `handlePrint` `_paidForPrint` | Fell back to `total` when paid was 0 | Tendered cash is passed only when the bill has no due and cash exceeds the total | `PrintContext.*` |
| F7 | Cart vs saved bill differed by 1 paisa in ~1.2% of cases | `calcPrice` | Float `toFixed` vs Python round-half-even | Exact BigInt decimal, half-up | `test_frontend_backend_rounding_parity` (the real `script.js` code is executed in Node) |
| F12 | Shortcuts hijacked typing | Central keydown dispatcher | Configurable shortcuts were dispatched before the input guard | Inside inputs, plain keys and Ctrl+C/V/X/A/Z/Y are never taken | Code trace (gate G1) |

**How to reproduce F2 (original code):** set the printer to an invalid name → press F3 → press F3 again → two bills are created. Bills 137 and 138 in the shipped DB are identical 6-item ₹810 bills 38 s apart, which matches this pattern.

**WARNING → FIXED (non-blocking)**
- User-entered names were inserted with `innerHTML` unescaped. A customer name containing `'` broke the Collect Payment button (JS syntax error). An `_h()` escape helper is now used.
- Package cart changes did not invalidate the stale server total.
- Print toasts said "printed" when the job had only been handed to the spooler.

---

## 4. Backend

| # | FAIL | Reproduce (original) | Fix | Verified |
|---|---|---|---|---|
| F3 | One crashed request locked the DB for the next bill | POST `/bill` with `customer_name:null` (→ 500), then a valid bill → `sqlite3.OperationalError: database is locked` | Validation before any write, one `BEGIN IMMEDIATE` transaction, rollback on error, `finally: close()` | `test_no_database_lock_after_bad_request`, `test_failure_inside_transaction_rolls_back_everything` |
| F6 | Invalid input accepted or 500 | Negative/NaN weight → ₹0 bill. Unknown product → silently dropped (bill #144 saved with 0 items). `paid=-500` → due ₹590. Credit with no phone → uncollectable due. `weight:"abc"` → 500 | `_validate_bill_payload()` returns 400 and writes nothing | `test_bad_payloads_write_nothing_and_never_500` |
| — | Partial commits | Customer/star updates were `commit()`ed before the bill insert | Removed the intermediate commits (single transaction) | Rollback test above |

**Also fixed:**
- Admin product, package and stock endpoints accepted `NaN`, negative and non-numeric prices. They now return 400.

---

## 5. Database

**Schema (unchanged except two added columns):**

| Table | Notes |
|---|---|
| `products` | PK `id`. Also has `serial_no` and 3 rate columns. |
| `bills` | PK `id` (the sequential bill number). Items are stored as serialized JSON, not normalized: this is intentional and was left as is. Added `loyalty_discount` and `client_bill_ref`, plus a UNIQUE partial index on `client_bill_ref`. |
| `customers` | PK `phone`. |
| `payment_history` | AUTOINCREMENT key. |
| `packages` | Items stored as JSON. |
| `loyalty_settings` | Single row. |
| `app_settings` | Key/value. |

**Other findings:**
- There are no foreign keys; relationships are by value (`bills.customer_phone`, `payment_history.customer_id`). Documented, not redesigned.
- **New indexes:** `bills(customer_phone)` and `payment_history(customer_id)`.
- **Migrations:** the column-adding migrations no longer swallow every exception; only "duplicate column" is ignored.
- **Integrity:** `PRAGMA integrity_check` = ok on the shipped DB. `init_db()` on the real DB is idempotent (table hashes identical across two runs).

**F8 FIXED — date sorting.**
- **Problem:** `created_at` is stored as `DD/MM/YYYY HH:MM:SS`, and `ORDER BY created_at` sorts it by day-of-month.
- **Reproduced:** 30/08 was listed above 01/10.
- **Affected:** the bill list, the customer bill list, the Excel export, the analytics cutoff (wrong bills counted), and "oldest unpaid bill first" payment allocation.
- **Fix:** a chronological SQL expression (`CREATED_AT_SORT_SQL`).
- **Test:** `test_bills_sorted_chronologically`.

**FIXED — deleted products came back.**
- **Problem:** the seed step ran whenever there were fewer than 100 products, so deleted products were re-inserted on restart.
- **Fix:** it now runs only when the table is empty.
- **Test:** `test_deleted_product_not_resurrected`.

---

## 6. Billing Calculations

Trace: product → rate (by price mode or temp override) → `line_amount(rate, weight_g)` (exact decimal, half-up) → subtotal → loyalty discount → total → paid (≥0, capped at total) → due → change.

**PASS** (tested):

| Case | Expected | Result |
|---|---|---|
| Single item | ₹90 | ₹90 |
| Multiple items / decimal qty (250 g + 1234.5 g @ ₹90) | 22.50 + 111.11 = ₹133.61 | ₹133.61 |
| Large quantity (500 kg) | ₹45,000 | ₹45,000 |
| Rounding tie (₹50.5 × 250 g) | ₹12.63 on screen and saved | ₹12.63 (was ₹12.62 before the fix) |
| Repeated creation | Unique sequential numbers | 12 concurrent saves gave 12 consecutive numbers |

**Backend authority:** client totals are ignored. Client prices are used only as an explicit `temp_price_per_kg` override (a feature), or as `expected_price_per_kg`, which is only a mismatch check.

**F1 and F7:** see §3.

---

## 7. Customers

**PASS**
- Customers are created on the first bill.
- Search works.
- Admin create enforces 10–15 digits and returns 409 on a duplicate phone.
- Bill history works.

**F9 FIXED — phone not normalised.**
- **Problem:** `/bill` stored the phone exactly as typed (`+91 98400 00002`), while lookup strips it to digits. One person became two customer records.
- **Fix:** phones are now stored as digits only and validated as 10–15 digits.
- **Test:** `test_phone_is_normalised`.

**FIXED — deleting a customer who owed money.** This silently orphaned the due. It now returns 409.

**Due consistency (PASS):** after 2 credit bills and 1 collection, `customers.total_due` = sum of `bills.due_amount` = ₹40, and `payment_history` = ₹100 (`test_customer_due_matches_bill_records`).

**Existing data (WARNING):** the DB has a 9-digit customer, `996248009`. It is not modified; billing with that number is now rejected until it is corrected.

---

## 8. Payments

Supported methods: cash, UPI, credit (the UI), plus card (API only).

| Case | Paid | Due | Status | Test |
|---|---|---|---|---|
| ₹100 bill, pay 100 | 100 | 0 | PAID | `test_full_payment` (on a ₹90 bill) |
| Pay 50 | 50 | 50 | CREDIT (phone required) | `test_partial_payment…` |
| Pay 150 | capped | 0 | PAID, change 60 | `test_overpayment…` |
| Pay 0 | 0 | full | CREDIT | `test_zero_payment…` |
| Negative / NaN / text | — | — | 400, nothing written | `test_negative_or_nan…` |

Negative due or change cannot occur: paid is ≥0 and capped at the total, and change on the receipt is clamped to ≥0.

**WARNING (by design, non-blocking):**
- In Cash/UPI mode the frontend sends `paid = total` regardless of the cash typed in.
- If a cashier types cash lower than the total in cash mode, the bill is still recorded as fully PAID.
- The cashier must switch to Credit for a partial payment.
- Recommendation: block or warn in cash mode when cash < total. Not changed without the owner's decision.

---

## 9. Loyalty

**PASS**
- Earn a star when the bill total ≥ the threshold.
- Redeem only when the cashier applies it, stars ≥ required, and the total ≥ the threshold.
- Stars decrease on redeem.
- A failed or invalid bill earns nothing: star/customer updates are now inside the bill transaction, where they were previously committed early.
- Settings are read live on every bill.

**Tested:** `test_earn_redeem_and_failed_bill_gives_nothing` (₹540 → 1 star; reward bill → ₹440 with ₹100 discount; stars back to 0).

**F5 FIXED — loyalty discount missing from the record.**
- The discount was not stored, so on the receipt the item sum was greater than TOTAL with no explanation.
- It is now stored in `bills.loyalty_discount` and printed as Subtotal and Discount.

**WARNING:**
- There is no bill cancellation/void feature. "Cancelled bills retain points" cannot occur through the UI.
- Deleting bills via Clear Analytics does **not** reverse stars or dues.

---

## 10. Products

**PASS**
- Create, edit and delete work, including Tamil, English and Tanglish names.
- Tanglish is auto-generated.
- Duplicate English names are allowed (the shipped DB has 5, e.g. "Honey"); products are distinguished by id.
- A missing image falls back to the default.

**FIXED**
- Price validation (NaN, negative or text are rejected).
- Image uploads are restricted to png/jpg/gif/webp. Before, `x.html` was written into `frontend/images` and served from the app's own address.

**WARNING**
- In a PyInstaller EXE, uploaded images are written into the temporary `_MEIPASS` folder and are lost on restart. This does **not** affect the current `run.bat` deployment, which runs from source.
- One product (`p0101002d`, "millet") has no Tamil name, so Tamil receipts will show its English name.

---

## 11. Packages

**PASS (code trace + tests)**
- Package billing uses the package's `price_per_kg` × the sum of the ingredient quantities.
- Unknown package → 400 (previously silently skipped).
- Package line amounts use the same exact rounding on both sides.

**WARNING (not deeply tested):**
- Package nutrition overrides, image upload and ingredient editing were only code-traced.
- If a product used as an ingredient is deleted, the package still bills correctly, because ingredient data is stored inside the package.

---

## 12. English Printing

**PASS**
- **Layout:** 48 columns, Font A.
- **Command sequence (byte check):** `ESC M 0` first, then `ESC E 1`, and it ends with `ESC d 6` + `GS V 0`.
- **Content:** every line ≤48 characters and pure ASCII ("Rs.", not ₹).
- **Regression:** English ESC/POS bytes for 6 fixed receipts (paid exact, paid with change, UPI; normal and estimate) are **byte-identical** to the pre-audit baseline (`test_matches_pre_audit_baseline`, SHA-256).

**Changed only when applicable:**
- New lines `Subtotal`/`Discount` (only when a loyalty discount > 0) and `Due` (only when due > 0).
- Credit bills print the saved paid amount (previously the total).

**WARNING:** the English text mode cannot print Tamil characters. A Tamil-only customer name, or a product with no English name, prints as `?` on English receipts.

---

## 13. Tamil Printing

**Pipeline:** `_build_print_context` → `TamilReceiptRasterRenderer` → HarfBuzz shaping → FreeType rasterisation → PIL "L" bitmap → python-escpos `image(impl='bitImageRaster')` (`GS v 0`) → transport.

**PASS**
- **Width:** 576 px at 80 mm and 384 px at 58 mm.
- **Margins:** 6 px.
- **Anchors:** fixed pixel anchors for Qty and Amount.
- **Height:** dynamic (1 item = 731 px; 40 items = 3,149 px; no clipping).
- **No forbidden techniques** in the print path: no wcwidth, no space padding, no global shrink.
- **Glyphs:** Noto Sans Tamil Bold has glyphs for ₹, digits and Latin. Only ★ is missing, and it is in an unreachable branch.
- **Mixed text:** mixed Tamil/English, Tamil + numbers and ₹ render correctly (visual check of `/tmp/tamil_edge.png`).
- **Due:** printed as நிலுவை.
- **Tamil is never sent as Unicode text:** asserted by `test_tamil_escpos_is_raster`.
- **Preview and production use the same renderer class** (`test_preview_uses_production_renderer`).

**F10 FIXED — unbroken Tamil names overlapped the columns.**
- **What:** a product name with no spaces overprinted Qty and Amount (e.g. வேர்க்கடலை×4, 665 px in a 265 px column).
- **Why:** word wrap only broke at spaces.
- **Fix:** overlong words now break between grapheme clusters (a base letter plus its vowel sign or virama are kept together).
- **Test:** `test_no_line_exceeds_name_column` (the text survives intact, and no line starts with a stranded mark).

**FIXED — PyInstaller EXE font path.** The bundled font lives at `_MEIPASS/backend/fonts`, but the code looked in `_MEIPASS/fonts`, so it silently fell back to the Windows Nirmala font.

**PHYSICAL PRINTER VALIDATION: PENDING.**

---

## 14. Printer Transport

**Routing** (by settings `mode`):

| Mode | Behaviour |
|---|---|
| `escpos` | USB (VID:PID) if set; otherwise Win32Raw with the named printer; otherwise the Windows default printer |
| `qz` | Returns base64 for QZ Tray in the browser |
| `browser` | Opens the print URL |
| `preview` | Preview only |

**PASS — failures are reported as failures:**
- A send failure → HTTP 500 with `success:false`, and the toast shows the reason.
- The bill is never rolled back because a print failed (`test_print_failure_is_reported`).
- Retrying Print reuses the same bill (F2).
- Tested without a printer: "No printer configured" / "Win32Raw requires Windows" → 500.

**FIXED:** the success message now says "Sent to printer" instead of "Printed successfully". The spooler or USB accepting the data does not prove paper came out.

**WARNING:**
- If no printer name is set, it silently uses the Windows default printer. On the dev machine that was "Microsoft Print to PDF", where the job "succeeds" and nothing prints. **Set the Epson name explicitly** (§28).
- USB mode uses python-escpos defaults (out endpoint 0x01). **NOT TESTABLE WITHOUT HARDWARE.**
- Ethernet: there is no network transport in code. Use Win32Raw via a Windows TCP/IP printer port.
- QZ Tray: **NOT TESTABLE WITHOUT HARDWARE / QZ install.**

---

## 15. Printer Diagnostics

**Present:** English test receipt (`/api/printer/test`), Tamil glyph test and raw raster test (`hardware-test/raw-raster`), full Tamil bill (`hardware-test/full-bill`), printer info (`/api/printer/info`), 48-column ruler, PNG and .bin artifacts, and a 12-item operator checklist.

**F11 FIXED — unearned physical PASS.**
- **What was shipped:** `debug_output/client_tamil_hardware_validation.json` already said "PHYSICAL PRINTER VALIDATION: PASS". The newer test metadata showed the last test went to "Microsoft Print to PDF" and failed with `SEND_FAILED`, so the PASS was not supported by any successful print.
- **Other problems:**
  - Confirmation did not require a successful print.
  - The test endpoints returned `success:true` even when the send failed.
  - Artifact paths depended on the current working directory.
- **Fixes:**
  - The artifacts were deleted.
  - Confirm returns 409 unless the latest test was SENT on the currently configured printer.
  - Status recomputes PASS only when the confirmation matches that latest test and printer.
  - Test responses carry `physical_printer_validation: "PENDING"`, and `success` reflects whether the send worked.
- **Test:** `test_physical_validation_stays_pending_without_printed_test`.

**Current state:**
- SOFTWARE VALIDATION: PASS
- **PHYSICAL PRINTER VALIDATION: PENDING**

---

## 16. Backup & Restore

**PASS (tested)**
- Backups use the SQLite online-backup API.
- **Restore round-trip:** products, customers, bills, payment_history, app_settings and loyalty_settings are **identical** after copying the latest backup into a temp DB. The production DB was never overwritten.
- A missing backup directory is recreated.

**FIXED**

| Issue | Now |
|---|---|
| Backups not verified | `integrity_check` before the file is kept; failure is logged and `False` is returned |
| Only 7 files ≈ 7 hours of history | Newest 48 plus the newest per day for 30 days (~50 MB at the current size) |
| Pruning by filename sort — a stray or corrupt file could push real backups out | Only files matching the app's own name pattern are ever pruned |
| No restore path | `tools/restore_backup.py`: `--list`, `--verify-all`, `--latest`, or a named file. Refuses to run while the app is up, verifies first, saves the current DB aside, swaps atomically. |
| Bill deletion (Clear Analytics) had no safety net | Takes a `pre_clear` backup first and aborts if that backup fails |
| `download-db` used `shutil.copy2` of a live DB | Consistent snapshot |

**WARNING:** backups are stored on the same disk as the database. Copy `backend/db_backups` to a USB drive or cloud folder weekly (§28).

---

## 17. Authentication & Security

**PASS**
- All routes except `/health` and `/login` require a session (tested on 6 representative routes).
- All SQL is parameterized (no string-built user input; `CREATED_AT_SORT_SQL` is a constant).
- File serving uses `send_from_directory` and `secure_filename`; no path traversal.
- Flask debug mode is off. Bound to 127.0.0.1 only.

**FIXED**
- `/debug/tamil-raster?bill_id=N` was public and exposed customer names and phone numbers. Hardware tests were public and could be triggered by any local web page.
- CORS was `*` and is now localhost only.
- The session cookie is now explicitly `SameSite=Lax` and HttpOnly.
- Upload file types are whitelisted.

**WARNING**
- The password is SHA-256 without a salt, with no lockout. The password is `1` (**gate G2**).
- `.secret_key` from the dev machine was in the project. It is excluded from the package; a new key is generated on the first run.

---

## 18. API Validation

There are about 80 endpoints (listed with `app.url_map`; see `backend/app.py` and `printer_routes.py`).

**Critical write endpoints hardened:** `/bill`, admin product/package/stock endpoints, customer delete, image upload.

**Malformed JSON on 7 POST endpoints never returns 500** (`test_malformed_json_never_500`).

**WARNING — not every endpoint was individually fuzzed:**
- `/api/loyalty-settings` POST: `float("abc")` → 500. Admin-only, low risk.
- `/api/customer/<phone>/collect-payment` with a non-numeric amount → 500.
- These give an error response and change no data.

---

## 19. Failure Recovery

| Scenario | Result |
|---|---|
| Crash in the middle of saving a bill | Full rollback: no bill, no stock/customer/loyalty change, no lock left (tested) |
| App restart | Bill counter and data persist; `init_db` is idempotent (tested) |
| Printer disconnected | Bill saved, print error shown, retry prints the same bill (server side tested; UI is gate G1) |
| Network lost mid-save | Same `client_bill_ref` on retry, so no duplicate (tested server side) |
| DB init failure | The app now shows the error and log path instead of opening on a broken DB |
| Missing font | Falls back through the bundled font and Windows fonts; a clear error if none exists |
| Missing backup dir | Recreated (tested) |
| Corrupt backup | Detected by `--verify-all`; skipped by `--latest` (tested) |
| Errors invisible under pythonw | Now written to a rotating log file, `backend/logs/billing_app.log` |

---

## 20. Performance

Run at about 13× the current data: 1,227 products, 3,003 bills, 303 customers.

| Operation | Time |
|---|---|
| GET /products | 104 ms |
| GET /bills | 147 ms |
| GET /summary | 41 ms |
| Customer search | 2 ms |
| Save 5-item bill | 3 ms |
| Save 40-item bill | 7.5 ms |
| English ESC/POS, 40 items | 40 ms |
| Tamil raster + ESC/POS, 40 items | 158 ms |
| Verified backup | 22 ms |
| `init_db` | 136 ms |

**No bottlenecks.** Nothing was optimized.

---

## 21. Installation

**PASS**
- Clean-directory startup creates the DB, image and backup directories, and the secret key.
- `/health` returns 200.
- `desktop_app.spec` bundles the fonts, uharfbuzz and freetype, and `verify_build.py` checks them.

**FIXED:** `run.bat` ran `pip install` on every launch, which needs internet at every start. It now installs only if an import check fails, and it aborts with a message if the install fails.

**WARNING (environment dependencies):**
- **Python:** `run.bat` requires the Python 3.10 launcher (`py -3.10`).
- **Internet:** needed once, for the first dependency install.
- **Browser engine:** WebView2 runtime is required (included in Windows 10/11).
- **Printer:** needs the Epson driver, or a Generic/Text-only driver on the correct port.
- **pywin32:** required for Win32Raw.
- **USB mode:** needs a libusb/WinUSB driver.
- **Fresh install catalogue:** a brand-new DB seeds only 97 products (the shipped DB has 227). **Deploy with the shipped `billing.db`**, not a fresh one.
- **Bundled venv:** `.venv` in the original zip is a Linux Python 3.14 environment from the dev machine. It is useless on Windows and excluded from the package.

---

## 22. Automated Tests

`tests/test_production_audit.py`: 44 tests, standard library only, no printer needed. It runs on a temp copy of the project.

**Coverage:** billing math, rounding parity with the real `script.js`, price modes, payments, due consistency, validation, transaction rollback, DB lock, bill numbering under concurrency, idempotency, deleted-number reuse, loyalty, ordering, print context, English ESC/POS (commands plus byte baseline), Tamil raster (width, height, wrapping, raster-only, same renderer), backup/restore, auth, upload whitelist, malformed JSON, diagnostics PENDING, print-failure handling, startup idempotence, seed resurrection, font fallback.

| Code under test | Result |
|---|---|
| **Fixed code** | **44 / 44 OK** |
| **Original code** | 26 fail (22 failures, 4 errors), proving the tests detect the defects |

---

## 23. Remaining Warnings

None of these block deployment; each is covered in the section named.

1. Cash mode with cash typed below the total is recorded as PAID (§8). Owner's decision.
2. English receipts can't print Tamil-only names (§12).
3. Silent fallback to the Windows default printer (§14). Mitigated by the checklist.
4. Backups are on the same disk (§16).
5. Unsalted password hash, no lockout (§17).
6. Loyalty settings and collect-payment endpoints return 500 on non-numeric input (§18).
7. No bill void/cancel feature; Clear Analytics does not reverse stars or dues (§9).
8. The Tamil HTML *browser fallback* preview does not show Due or Discount. The thermal raster does.
9. EXE builds lose uploaded images (§10). Not applicable to `run.bat`.
10. Data quality: one 9-digit phone, one product without a Tamil name, 5 duplicate English names.
11. Packages were only partially tested (§11).
12. UI responsiveness and layout at different resolutions: **not tested** (no display available).

---

## 24. Remaining Blockers

- **G1:** WebView2 smoke test of the changed frontend.
- **G2:** change the password from `1`.
- **G3:** remove the test bills and customers.
- **G4:** physical printer validation (English and Tamil) on the client's printer.

**PHYSICAL PRINTER VALIDATION: PENDING.**

---

## 25. Files Modified

Source changes (+1,672 / −192 lines, excluding deleted artifacts):

| File | Change |
|---|---|
| `backend/app.py` | Money helpers; validation; transactional + idempotent `save_bill`; price modes; migrations and indexes; chronological ordering; print-context paid/due/discount; customer-delete guard; backup rewrite and pre-clear backup; auth whitelist; cookies/CORS; admin validation; upload whitelist; DB snapshot download; seed rule |
| `backend/printer_routes.py` | Diagnostics PENDING logic, debug dir anchoring, honest status; receipt logs moved to DEBUG |
| `backend/services/tamil_text_render.py` | Grapheme-safe wrapping; frozen font path |
| `backend/services/receipt_formatter.py` | Subtotal/Discount/Due lines (conditional) |
| `backend/services/escpos_generator.py` | Frozen font path; receipt logs moved to DEBUG |
| `backend/services/thermal_printer.py` | "Sent to printer"; receipt logs moved to DEBUG |
| `frontend/script.js` | Idempotent save, price mode, exact rounding, credit print fix, honest messages, shortcut guard, generated help, discard confirm, HTML escaping |
| `desktop_app.py` | Rotating log file; fatal and visible DB-init failure |
| `run.bat` | Conditional dependency install |

**Added:** `tools/restore_backup.py`, `tests/test_production_audit.py`, `tests/gen_outputs.py`, `tests/baseline/*`, this report.

**Removed:** `frontend/script.js.bak`, `debug_output/*`, `_pyver.txt`, `.venv/` (not packaged), `backend/.secret_key` (not packaged).

**Not modified:** `billing.db` (its schema migrates automatically on first start), templates, CSS, `index.html`, Tamil keyboard/transliteration, `nutrition_engine.py`, the English layout code paths.

---

## 26. Tests Executed

1. Static reading of all backend, service and frontend flows.
2. API probes P1–P14 on a real-DB copy (original code). These reproduced F1, F3, F6, F7, F8 and F9.
3. Rounding fuzz: 1,961,000 price/weight pairs, frontend JS vs backend (original: 23,687 mismatches).
4. Font glyph coverage (HarfBuzz notdef check).
5. Tamil raster edge-case render plus visual inspection.
6. English ESC/POS baseline capture (pre-change) and byte inspection.
7. Backup → restore round-trip, corrupt file, missing directory.
8. `init_db` idempotence on the real DB (clean dir, existing DB, second run).
9. The 44-test suite on the fixed code (OK) and the original code (26 fail).
10. Performance run (§20).
11. `node --check frontend/script.js`; `py_compile` on all changed Python files.

---

## 27. Exact Commands Used

```bash
# isolated environments (never the real DB)
./mkenv.sh /tmp/p1 keepdb            # copy of project incl. real billing.db
# regression suite
python3 -m unittest discover -s tests -v           # fixed code  → Ran 44 … OK
cd /tmp/orig_check/app && python3 -m unittest discover -s tests   # original → FAILED (failures=22, errors=4)
# frontend syntax
node --check frontend/script.js
# restore tool
python3 tools/restore_backup.py --verify-all
# rounding fuzz
node fz.js && python3 compare.py     # 1,961,000 cases
```

On the store PC:

```bat
py -3.10 -m unittest discover -s tests -v
py -3.10 tools\restore_backup.py --verify-all
```

---

## 28. Final Deployment Checklist

**Install**
1. ☐ Copy the package to the store PC. Do **not** copy `.venv`.
2. ☐ Install Python 3.10 (with the py launcher) and run `run.bat` once, with internet, so it can install dependencies.
3. ☐ Run `py -3.10 -m unittest discover -s tests` → must end with `OK`.
4. ☐ Log in with `1` and **change the password immediately** (G2).

**WebView2 smoke test (G1)**
5. ☐ Add 2 items and press F3 with the printer **switched off** → the toast says "Bill #N is SAVED but did not print". Turn the printer on and press F3 → the **same** bill #N prints. Admin → Bills shows only one #N.
6. ☐ Double-click Save quickly → only one bill is created.
7. ☐ Switch to Wholesale, add Red Foxtail Millet 1 kg → cart shows ₹75, and the receipt and the saved bill both show ₹75.
8. ☐ Credit mode, paid 0, no phone → "Customer phone is required". With a phone → the receipt shows Paid Rs.0.00 and Due.
9. ☐ Type "ragi" and copy/paste inside the search box → typing and Ctrl+C work normally. F7 (help) shows the actual configured keys.

**Printer (G4)**
10. ☐ Admin → Printer: set the Epson printer **by name** (not the default). Mode ESC/POS, 80 mm.
11. ☐ Print the English test receipt → check 48 columns, bold, clean cut.
12. ☐ Hardware test → Full Tamil Bill → inspect the paper → complete the 12-item checklist → status becomes PHYSICAL PRINTER VALIDATION: PASS.
13. ☐ Print one real English bill and one real Tamil bill with a long product name.

**Data (G3)**
14. ☐ Remove test bills 137–139 and the test customers (Clear Analytics takes an automatic backup first). Correct the customer with the 9-digit phone.
15. ☐ Confirm `backend/db_backups` gains a new verified file every hour, and `backend/logs/billing_app.log` exists.
16. ☐ Weekly: copy `backend/db_backups` off the PC. Practise one restore with `tools\restore_backup.py --latest`, with the app closed, on a spare copy first.
