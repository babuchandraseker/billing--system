/**
 * Tamil Virtual Keyboard – Optional on-screen keyboard for direct Tamil character input.
 * Draggable floating panel. Works alongside the transliteration engine.
 */
const tamilKeyboard = (() => {

    const ROWS = [
        ['அ','ஆ','இ','ஈ','உ','ஊ','எ','ஏ','ஐ','ஒ','ஓ','ஔ'],
        ['க','ங','ச','ஞ','ட','ண','த','ந','ப','ம'],
        ['ய','ர','ல','வ','ழ','ள','ற','ன','ஜ','ஷ','ஸ','ஹ'],
        ['\u0bbe','\u0bbf','\u0bc0','\u0bc1','\u0bc2','\u0bc6','\u0bc7','\u0bc8','\u0bca','\u0bcb','\u0bcc','\u0bcd']
    ];

    const ROW_LABELS = ['உயிர்', 'மெய்', 'மெய்②', 'உயிர்மெய்'];

    let activeInputId = null;
    let panelEl = null;

    // Drag state
    let _drag = { on: false, startX: 0, startY: 0, origLeft: 0, origTop: 0 };

    function _makeDraggable(panel, handle) {
        handle.style.cursor = 'grab';

        handle.addEventListener('mousedown', (e) => {
            if (e.button !== 0) return;
            e.preventDefault();
            const rect = panel.getBoundingClientRect();
            _drag.on      = true;
            _drag.startX  = e.clientX;
            _drag.startY  = e.clientY;
            _drag.origLeft = rect.left;
            _drag.origTop  = rect.top;
            handle.style.cursor = 'grabbing';

            // Switch from bottom/transform positioning to absolute top/left
            panel.style.bottom    = 'auto';
            panel.style.transform = 'none';
            panel.style.left      = rect.left + 'px';
            panel.style.top       = rect.top  + 'px';
        });

        document.addEventListener('mousemove', (e) => {
            if (!_drag.on) return;
            const dx = e.clientX - _drag.startX;
            const dy = e.clientY - _drag.startY;
            let newLeft = _drag.origLeft + dx;
            let newTop  = _drag.origTop  + dy;
            // Clamp to viewport
            const pw = panel.offsetWidth, ph = panel.offsetHeight;
            newLeft = Math.max(0, Math.min(window.innerWidth  - pw, newLeft));
            newTop  = Math.max(0, Math.min(window.innerHeight - ph, newTop));
            panel.style.left = newLeft + 'px';
            panel.style.top  = newTop  + 'px';
        });

        document.addEventListener('mouseup', () => {
            if (_drag.on) {
                _drag.on = false;
                handle.style.cursor = 'grab';
            }
        });
    }

    function _createPanel() {
        if (panelEl) return panelEl;
        panelEl = document.createElement('div');
        panelEl.id = 'tamil-kbd-panel';
        panelEl.style.cssText = `
            display:none;position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
            background:#1a1a2e;border:2px solid #e94560;border-radius:14px;
            padding:0 14px 14px;z-index:10001;max-width:620px;width:96%;
            box-shadow:0 8px 32px rgba(0,0,0,0.45);user-select:none;
        `;

        // Draggable header
        const hdr = document.createElement('div');
        hdr.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:10px 0 8px;margin-bottom:6px;border-bottom:1px solid #333;';
        hdr.innerHTML = `<span style="color:#e94560;font-weight:700;font-size:13px;">⌨️ தமிழ் Keyboard <span style="color:#666;font-size:11px;font-weight:400;">(drag to move)</span></span>`;
        const closeBtn = document.createElement('button');
        closeBtn.textContent = '✕';
        closeBtn.style.cssText = 'background:none;border:none;color:#aaa;font-size:18px;cursor:pointer;padding:0 4px;';
        closeBtn.onclick = () => hide();
        hdr.appendChild(closeBtn);
        panelEl.appendChild(hdr);

        // Make header the drag handle
        _makeDraggable(panelEl, hdr);

        // Rows
        ROWS.forEach((row, ri) => {
            const rowDiv = document.createElement('div');
            rowDiv.style.cssText = 'display:flex;gap:4px;justify-content:center;margin-bottom:4px;flex-wrap:wrap;';
            row.forEach(ch => {
                const btn = document.createElement('button');
                btn.textContent = ch;
                btn.type = 'button';
                btn.style.cssText = `
                    min-width:38px;height:38px;border:1px solid #333;border-radius:6px;
                    background:#16213e;color:#fff;font-size:16px;cursor:pointer;
                    transition:background 0.12s;font-family:'Noto Sans Tamil',sans-serif;
                `;
                btn.onmouseenter = () => btn.style.background = '#e94560';
                btn.onmouseleave = () => btn.style.background = '#16213e';
                btn.onmousedown = (e) => e.preventDefault(); // prevent field blur
                btn.onclick = () => _typeChar(ch);
                rowDiv.appendChild(btn);
            });
            panelEl.appendChild(rowDiv);
        });

        // Utility row
        const utilRow = document.createElement('div');
        utilRow.style.cssText = 'display:flex;gap:6px;justify-content:center;margin-top:6px;';
        [['⌫','backspace'],['Space','space'],['✓ Done','done']].forEach(([label, action]) => {
            const btn = document.createElement('button');
            btn.textContent = label;
            btn.type = 'button';
            btn.style.cssText = `
                padding:7px 18px;border:1px solid #333;border-radius:6px;
                background:${action==='done'?'#e94560':'#0f3460'};color:#fff;font-size:13px;cursor:pointer;
                font-family:var(--font,sans-serif);
            `;
            btn.onmousedown = (e) => e.preventDefault();
            btn.onclick = () => {
                if (action === 'backspace') _backspace();
                else if (action === 'done') hide();
                else _typeChar(' ');
            };
            utilRow.appendChild(btn);
        });
        panelEl.appendChild(utilRow);

        document.body.appendChild(panelEl);
        return panelEl;
    }

    function _typeChar(ch) {
        if (!activeInputId) return;
        const el = document.getElementById(activeInputId);
        if (!el) return;
        const start = el.selectionStart ?? el.value.length;
        const end   = el.selectionEnd   ?? el.value.length;
        el.value = el.value.substring(0, start) + ch + el.value.substring(end);
        el.selectionStart = el.selectionEnd = start + ch.length;
        el.focus();
        el.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function _backspace() {
        if (!activeInputId) return;
        const el = document.getElementById(activeInputId);
        if (!el) return;
        const pos = el.selectionStart;
        if (pos > 0) {
            el.value = el.value.substring(0, pos - 1) + el.value.substring(pos);
            el.selectionStart = el.selectionEnd = pos - 1;
            el.focus();
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }
    }

    function show(inputId) {
        activeInputId = inputId;
        if (typeof tamilTranslit !== 'undefined' && tamilTranslit.isEnabled(inputId)) {
            tamilTranslit.disable(inputId);
        }
        _createPanel();
        panelEl.style.display = 'block';
        _updateToggleBtn(inputId, true);
        // Don't steal focus — let user keep typing in the field
    }

    function hide() {
        if (panelEl) panelEl.style.display = 'none';
        if (activeInputId) {
            _updateToggleBtn(activeInputId, false);
            if (typeof tamilTranslit !== 'undefined') {
                tamilTranslit.enable(activeInputId);
            }
            activeInputId = null;
        }
    }

    function toggle(inputId) {
        if (activeInputId === inputId && panelEl && panelEl.style.display !== 'none') {
            hide();
        } else {
            show(inputId);
        }
    }

    function _updateToggleBtn(inputId, active) {
        const btn = document.getElementById(inputId + '-kbd-toggle');
        if (btn) {
            btn.textContent = active ? '⌨️ Keyboard ON' : '⌨️ Keyboard';
            btn.style.background = active ? '#fff3e0' : '';
            btn.style.color = active ? '#e65100' : '';
            btn.style.fontWeight = active ? '600' : '400';
        }
    }

    return { show, hide, toggle };
})();
