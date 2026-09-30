/*
 * Behavioural harness for the POS print flow in frontend/script.js.
 *
 *   node tests/js/print_flow_harness.js
 *
 * The real print functions are cut out of script.js and run in a sandbox with
 * a fake fetch, a tiny fake DOM, and spies on window.print / window.open.
 * Prints one JSON object {scenario: result} for tests/test_production_audit.py.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'frontend', 'script.js'), 'utf8')
    .replace(/\r\n/g, '\n');

/** Source of a top-level function: from its declaration to the first "\n}\n". */
function fnSource(name) {
    const m = new RegExp(`^(async )?function ${name}\\(`, 'm').exec(SRC);
    if (!m) throw new Error(`function ${name} not found in script.js`);
    const end = SRC.indexOf('\n}\n', m.index);
    return SRC.slice(m.index, end + 2);
}

/** The Ctrl+P keydown listener (registered at top level, so cut by marker). */
function ctrlPListenerSource() {
    const start = SRC.indexOf("window.addEventListener('keydown', (e) => {\n    if ((e.ctrlKey || e.metaKey)");
    if (start < 0) throw new Error('Ctrl+P listener not found');
    const end = SRC.indexOf('}, true);', start);
    return SRC.slice(start, end + '}, true);'.length);
}

const FUNCTIONS = [
    '_startPrintDeadline', '_printTimeoutMessage', '_readPrintResponse',
    'directThermalPrint', '_directThermalPrintOnce', 'showPrintErrorDialog',
    'handlePrint', 'openBillPreview', 'printViaQZTray', '_resolveBillLang',
];

class El {
    constructor(tag) {
        this.tagName = tag; this.children = []; this.style = {}; this.attrs = {};
        this.listeners = {}; this.textContent = ''; this.parent = null; this.id = '';
    }
    append(...c) { c.forEach(x => this.appendChild(x)); }
    appendChild(c) { c.parent = this; this.children.push(c); return c; }
    setAttribute(k, v) { this.attrs[k] = v; }
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
    remove() { if (this.parent) { this.parent.children = this.parent.children.filter(x => x !== this); this.parent = null; } }
    focus() {}
    find(pred) {
        if (pred(this)) return this;
        for (const c of this.children) { const r = c.find(pred); if (r) return r; }
        return null;
    }
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

/**
 * Build a sandbox. `server(url, opts, n)` scripts the fake fetch: return
 * {status, body} (body object → JSON, string → raw text), or 'hang' to never
 * answer (the request still honours AbortSignal like a real fetch).
 */
function sandbox({ server, save, timeoutMs = 2000 }) {
    const rec = {
        fetches: [], windowPrint: 0, windowOpen: [], toasts: [], dialogs: [],
        afterSuccess: 0, previewModal: [], settingsWrites: 0, keydownPrevented: 0,
    };
    const body = new El('body');
    const document = {
        body,
        createElement: tag => new El(tag),
        getElementById: id => body.find(e => e.id === id),
    };
    const fetch = (url, opts = {}) => {
        rec.fetches.push({ url, method: opts.method || 'GET' });
        if (url.includes('/api/printer/settings') && (opts.method || 'GET') !== 'GET') rec.settingsWrites++;
        const reply = server(url, opts, rec.fetches.length);
        return new Promise((resolve, reject) => {
            const abort = () => { const e = new Error('The operation was aborted.'); e.name = 'AbortError'; reject(e); };
            if (opts.signal) {
                if (opts.signal.aborted) return abort();
                opts.signal.addEventListener('abort', abort);
            }
            Promise.resolve(reply).then(r => {
                if (r === 'hang') return;
                const text = typeof r.body === 'string' ? r.body : JSON.stringify(r.body);
                resolve({ status: r.status || 200, ok: (r.status || 200) < 400, text: async () => text,
                          json: async () => JSON.parse(text) });
            });
        });
    };
    const ctx = {
        console: { log() {}, warn() {}, error() {} },
        setTimeout, clearTimeout, AbortController, Promise, JSON, Math, String, Number, parseFloat, isNaN, Error,
        document, fetch,
        PRINT_TIMEOUT_MS: timeoutMs,
        cart: [{ id: 'x' }], isPrinting: false, _printOpSeq: 0, _pendingBrowserPrintBillId: null,
        _currentPaymentMethod: 'cash', billLang: 'en',
        _saveBillCore: save || (async () => ({ id: '101', total: 100, total_amount: 100, due_amount: 0 })),
        updateStarDisplay() {}, _updateLoyaltyAfterSave() {},
        showToast: m => rec.toasts.push(m),
        _afterSuccessfulPrint: () => { rec.afterSuccess++; },
        showReceiptPreviewModal: url => rec.previewModal.push(url),
    };
    vm.createContext(ctx);
    vm.runInContext(`var window = globalThis;
        window.print = () => { __rec.windowPrint++; };
        window.open = (url) => { __rec.windowOpen.push(url); return {}; };
        window.QZTrayPrint = { printRawEscpos: async () => {} };
        window.__listeners = {};
        window.addEventListener = (t, f) => { (window.__listeners[t] = window.__listeners[t] || []).push(f); };`,
        Object.assign(ctx, { __rec: rec }));
    // PRINT_TIMEOUT_MS is provided by the sandbox (short), so skip script.js's const.
    vm.runInContext(FUNCTIONS.map(fnSource).join('\n'), ctx);
    vm.runInContext(ctrlPListenerSource(), ctx);
    const dialog = () => {
        const d = body.find(e => e.id === 'print-error-modal');
        if (!d) return null;
        const pre = d.find(e => e.id === 'print-error-message');
        const head = d.find(e => e.tagName === 'div' && /^❌/.test(e.textContent));
        return { title: head && head.textContent, message: pre && pre.textContent };
    };
    const printCalls = () => rec.fetches.filter(f => f.url === '/api/thermal/print').length;
    return { ctx, rec, dialog, printCalls };
}

function summary(s, extra = {}) {
    return Object.assign({
        print_requests: s.printCalls(),
        window_print: s.rec.windowPrint,
        window_open: s.rec.windowOpen,
        preview_modal: s.rec.previewModal,
        after_success: s.rec.afterSuccess,
        settings_writes: s.rec.settingsWrites,
        other_fetches: s.rec.fetches.filter(f => f.url !== '/api/thermal/print').map(f => f.url),
        dialog: s.dialog(),
        is_printing: s.ctx.isPrinting,
    }, extra);
}

const escposOk = { status: 200, body: { success: true, mode: 'escpos', message: 'Sent to printer' } };
const escposFail = { status: 500, body: { success: false, mode: 'escpos', stage: 'printer', error_code: 'printer_error',
                                          message: 'WritePrinter: The device is not connected.' } };

const SCENARIOS = {
    async escpos_success() {
        const s = sandbox({ server: () => escposOk });
        await s.ctx.handlePrint(); await sleep(150);
        return summary(s);
    },
    async escpos_failure() {
        const s = sandbox({ server: () => escposFail });
        await s.ctx.handlePrint(); await sleep(300);   // well past any retry window
        return summary(s);
    },
    async html_error_page() {
        const s = sandbox({ server: () => ({ status: 500, body: '<!doctype html><title>500 Internal Server Error</title>' }) });
        await s.ctx.handlePrint(); await sleep(150);
        return summary(s);
    },
    async session_expired() {
        const s = sandbox({ server: () => ({ status: 401, body: { error: 'Unauthorized' } }) });
        await s.ctx.handlePrint(); await sleep(150);
        return summary(s);
    },
    async unexpected_server_error() {
        const s = sandbox({ server: () => ({ status: 500, body: { success: false, error_code: 'unexpected_error',
            message: 'Unexpected error in the billing software (reference ABCD1234).' } }) });
        await s.ctx.handlePrint(); await sleep(150);
        return summary(s);
    },
    async print_timeout_then_late_success() {
        // Server hangs; after the limit a late success must change nothing.
        let release;
        const late = new Promise(r => { release = r; });
        const s = sandbox({ timeoutMs: 250, server: () => late.then(() => escposOk) });
        await s.ctx.handlePrint();
        await sleep(400);
        const atTimeout = summary(s);
        release(); await sleep(50);
        return summary(s, { at_timeout: atTimeout });
    },
    async late_response_does_not_touch_newer_print() {
        // Attempt 1 times out; attempt 2 is in flight when attempt 1's save finally returns.
        let releaseSave;
        const slowSave = new Promise(r => { releaseSave = r; });
        let saves = 0;
        const s = sandbox({
            timeoutMs: 400,
            save: () => (++saves === 1 ? slowSave : Promise.resolve({ id: '102', total: 100, due_amount: 0 })),
            server: () => 'hang',
        });
        s.ctx.handlePrint();                     // attempt 1: its save hangs
        await sleep(500);                        // attempt 1 timed out while saving
        const afterFirst = summary(s);
        s.ctx.handlePrint();                     // attempt 2 starts, its print hangs
        await sleep(30);
        releaseSave({ id: '101', total: 100, due_amount: 0 });   // attempt 1's late save
        await sleep(150);                        // attempt 2's print request is now in flight
        return summary(s, { after_first: afterFirst, saves });
    },
    async save_timeout_never_prints() {
        let releaseSave;
        const s = sandbox({ timeoutMs: 250, save: () => new Promise(r => { releaseSave = r; }), server: () => escposOk });
        s.ctx.handlePrint(); await sleep(350);   // save hangs past the limit
        releaseSave({ id: '101', total: 100, due_amount: 0 }); await sleep(300);
        return summary(s);
    },
    async browser_mode_explicit() {
        const s = sandbox({ server: () => ({ status: 200, body: { success: true, mode: 'browser',
            print_url: '/print-bill/101?language=en&bill_type=normal&preview=1' } }) });
        await s.ctx.handlePrint(); await sleep(150);
        return summary(s, { pending_browser_bill: s.ctx._pendingBrowserPrintBillId });
    },
    async preview_mode() {
        const s = sandbox({ server: () => ({ status: 200, body: { success: true, mode: 'preview', preview: 'x' } }) });
        await s.ctx.handlePrint(); await sleep(150);
        return summary(s);
    },
    async reprint_has_own_timeout() {
        const s = sandbox({ timeoutMs: 250, server: () => 'hang' });
        const r = await s.ctx.directThermalPrint('77', null);
        return summary(s, { returned: r });
    },
    async ctrl_p_blocked() {
        const s = sandbox({ server: () => escposOk });
        let prevented = 0;
        for (const f of s.ctx.window.__listeners.keydown || []) {
            f({ ctrlKey: true, metaKey: false, altKey: false, key: 'p',
                preventDefault() { prevented++; }, stopPropagation() {} });
        }
        return summary(s, { prevented });
    },
};

(async () => {
    const out = {};
    for (const [name, fn] of Object.entries(SCENARIOS)) {
        try { out[name] = await fn(); } catch (e) { out[name] = { harness_error: String(e && e.stack || e) }; }
    }
    process.stdout.write(JSON.stringify(out));
})();
