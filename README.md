# Billing System v5 — Keyboard-First POS

A fully keyboard-operable billing/POS web application. No mouse required for any core operation.

---

## 🚀 How to Run

1. Install Python dependencies:
   ```bash
   cd backend
   pip install -r requirements.txt
   ```

2. Start the backend server:
   ```bash
   python app.py
   ```

3. Open `frontend/index.html` in your browser (or serve via the backend's static route).

4. **Thermal printer (optional):** see [THERMAL_PRINTER_SETUP.md](THERMAL_PRINTER_SETUP.md) — Admin → Printer, ESC/POS direct print on F3 (no browser dialog).

---

## 🖨️ Thermal Printing (v20+)

| Mode | Description |
|------|-------------|
| **ESC/POS Direct** | Python sends raw bytes to Windows printer (default) |
| **QZ Tray** | Raw ESC/POS via [QZ Tray](https://qz.io/download/) |
| **Preview** | Text receipt on screen only |
| **Browser** | HTML preview window (manual print) |

---

## ⌨️ Keyboard Shortcuts

| Key | Action |
|-----|--------|
| **F1** | Show keyboard help overlay |
| **F2** | Focus product search |
| **F3** | Print bill |
| **F4** | Clear cart (with confirmation) |
| **F6** | Focus customer name (auto-expands section) |
| **F7** | Focus payment / cash input |
| **F9** | Navigate to next bill in history |
| **F10** | Navigate to previous bill in history |
| **F11** | Hold current bill |
| **F12** | Retrieve held bill |
| **Z** | New bill — resets cart, customer, payment, focus |
| **Ctrl+N** | New bill (same as Z key) |
| **Ctrl+S** | Save bill to server |
| **Ctrl+X** | Delete current bill |
| **Ctrl+B** | Switch to billing page |
| **Ctrl+A** | Switch to admin page |
| **Ctrl+W** | WhatsApp bill |
| **Escape** | Close modal / clear search / reset focus |
| **↑ ↓ ← →** | Navigate product grid |
| **Enter** | Open selected product modal |
| **← →** (in modal) | Navigate between weight / unit / price fields |

---

## 🔧 Changes Applied (v5 → v5-patched)

### Task 1 — Modal Quantity Auto-Focus
- `openWeightModal()` now uses `setTimeout(() => { wt-input.focus(); wt-input.select(); }, 50)` instead of bare `.focus()` to ensure the input is reliably focused and selected after the modal renders.

### Task 2 — Focus Lock Inside Modal
- Added a `focusin` event listener that detects when focus escapes an open modal (`#wt-modal.open` or `#pkg-modal.open`) and immediately redirects it back to the quantity input. Appended once at end of `script.js`.

### Task 3 — No Customer Dropdown
- Verified: customer section already uses `<input id="customer-phone">` and `<input id="customer-name">` — no `<select>` elements. No change needed.

### Task 4 — F6 Shortcut (Focus Customer)
- Verified: F6 is mapped to `customerSearch → focusCustomer()` in the central shortcut dispatcher. `focusCustomer()` expands the collapsible section and focuses `#customer-name`. No duplicate handlers.

### Task 5 — Z Key → New Bill
- Added a dedicated `keydown` listener for the `z` key (appended at end of `script.js`).
- Guards: does not fire when an `INPUT`/`TEXTAREA`/`SELECT` is active, or when a modal is open.
- Resets: `cart`, `customer-name`, `customer-phone`, `cash-input`, loyalty flags, repeat-bill preview.
- Calls `resetFocus()` and shows toast `🆕 New Bill`.

### Task 6 — No Drag Logic
- Verified: no `drag`, `mousemove`, or `mousedown` product-selection code found in `script.js`. Keyboard arrow navigation already fully implemented via `_highlightCard()` and `_getVisibleCards()`.

### Task 7 — Central Shortcut Manager
- Verified: a single `document.addEventListener('keydown', ...)` handles all shortcuts via `_getShortcutHandlers()` dispatch. F2, F3, F4, F6, F7, Escape all covered. No duplicate global listeners.

### Task 8 — No Conflicts
- Verified: only one global keydown handler. No dropdown JS. No drag logic. One focus system.

---

## 📂 File Structure

```
billing_v5/
├── frontend/
│   ├── index.html          # Main UI (no select dropdowns for customer)
│   ├── script.js           # All JS — patched with Tasks 1, 2, 5
│   ├── style.css           # Styles (unchanged)
│   ├── tamil-keyboard.js   # Tamil input support
│   ├── tamil-transliterate.js
│   └── images/             # Product images
├── backend/
│   ├── app.py              # Flask API server
│   ├── nutrition_engine.py
│   ├── billing.db          # SQLite database
│   ├── requirements.txt
│   └── product_images/
├── run.bat                 # Windows quick-start
└── README.md               # This file
```

---

## 🖥️ Full Keyboard-Only Operation Flow

1. **Start** → Focus lands on product search (F2)
2. **Find product** → Type name or use ↑↓←→ to navigate grid
3. **Select product** → Press Enter → Weight modal opens, quantity auto-focused
4. **Enter quantity** → Type value → Press Enter to add to cart
5. **Add customer** → Press F6 → Type name / phone
6. **Payment** → Press F7 → Type cash amount
7. **Print** → Press F3
8. **New bill** → Press Z (or Ctrl+N)

No mouse required at any step.

# custom-billing-pos
