/**
 * Tamil Transliteration Engine (Client-side, no external API)
 * Type English phonetically → get Tamil text in real-time
 * Uses a buffer approach: captures keystrokes, converts on space/Enter/punctuation.
 */
const tamilTranslit = (() => {

    // ─── Tamil phonetic mapping ─────────────────────────
    const CONSONANTS = {
        'kh':'க','gh':'க','ng':'ங','ch':'ச','chh':'ச','sh':'ஷ','shh':'ஷ',
        'nj':'ஞ','ny':'ஞ','th':'த','dh':'த','nh':'ண','nn':'ன','nnn':'ன',
        'ph':'ப','bh':'ப','rr':'ற','ll':'ள','zh':'ழ',
        'k':'க','g':'க','c':'ச','s':'ச','j':'ஜ',
        't':'த','d':'த','n':'ந',
        'p':'ப','b':'ப','m':'ம',
        'y':'ய','r':'ர','l':'ல',
        'v':'வ','w':'வ','h':'ஹ',
        'f':'ஃப','x':'க்ஷ','z':'ஜ','q':'க'
    };

    const VOWELS_STANDALONE = {
        'aa':'ஆ','ai':'ஐ','au':'ஔ','ee':'ஏ','ii':'ஈ','oo':'ஓ','uu':'ஊ','ou':'ஓ',
        'a':'அ','i':'இ','u':'உ','e':'எ','o':'ஒ'
    };

    const VOWEL_MARKS = {
        'aa':'\u0bbe','ai':'\u0bc8','au':'\u0bcc','ee':'\u0bc7','ii':'\u0bc0',
        'oo':'\u0bcb','uu':'\u0bc2','ou':'\u0bcb',
        'a':'','i':'\u0bbf','u':'\u0bc1','e':'\u0bc6','o':'\u0bca'
    };

    const VIRAMA = '\u0bcd';

    // Sorted keys for greedy matching
    const cKeys = Object.keys(CONSONANTS).sort((a,b) => b.length - a.length);
    const vKeys = Object.keys(VOWELS_STANDALONE).sort((a,b) => b.length - a.length);

    function transliterateWord(word) {
        if (!word) return '';
        let result = '';
        let i = 0;
        const low = word.toLowerCase();

        while (i < low.length) {
            // Try consonant
            let cMatch = null, cLen = 0;
            for (const ck of cKeys) {
                if (low.startsWith(ck, i)) { cMatch = ck; cLen = ck.length; break; }
            }

            if (cMatch) {
                const tamilCons = CONSONANTS[cMatch];
                i += cLen;

                // Look for following vowel
                let vMatch = null, vLen = 0;
                for (const vk of vKeys) {
                    if (low.startsWith(vk, i)) { vMatch = vk; vLen = vk.length; break; }
                }

                if (vMatch) {
                    result += tamilCons + VOWEL_MARKS[vMatch];
                    i += vLen;
                } else {
                    // Consonant with virama (no vowel follows)
                    result += tamilCons + VIRAMA;
                }
                continue;
            }

            // Try standalone vowel
            let vMatch = null, vLen = 0;
            for (const vk of vKeys) {
                if (low.startsWith(vk, i)) { vMatch = vk; vLen = vk.length; break; }
            }
            if (vMatch) {
                result += VOWELS_STANDALONE[vMatch];
                i += vLen;
                continue;
            }

            // Pass through digits, punctuation, etc.
            result += word[i];
            i++;
        }
        return result;
    }

    // ─── Field management ─────────────────────────
    const fields = {};  // id → { enabled, buffer, el }

    function _getState(inputId) {
        if (!fields[inputId]) {
            fields[inputId] = { enabled: false, buffer: '', composing: false };
        }
        return fields[inputId];
    }

    function enable(inputId) {
        const el = document.getElementById(inputId);
        if (!el) return;
        const state = _getState(inputId);

        if (!state.initialized) {
            // Fix 3: Track IME composition (native Tamil keyboard via OS IME)
            // When composing, the transliteration engine MUST NOT intercept keystrokes
            el.addEventListener('compositionstart', () => { state.composing = true; });
            el.addEventListener('compositionend',   () => { state.composing = false; state.buffer = ''; });

            // Buffer approach: capture keys, transliterate buffer on space/Enter/punctuation
            el.addEventListener('keydown', (e) => {
                if (!fields[inputId]?.enabled) return;
                // Fix 3: Never intercept if native IME/OS input method is composing
                if (e.isComposing || state.composing) return;

                const key = e.key;

                // Space, Enter, or punctuation → flush buffer
                if (key === ' ' || key === 'Enter' || key === ',' || key === '.' || key === ';' || key === '/' || key === '-') {
                    if (state.buffer) {
                        e.preventDefault();
                        const tamil = transliterateWord(state.buffer);
                        _insertAtCursor(el, tamil + (key === 'Enter' ? '\n' : key));
                        state.buffer = '';
                        return;
                    }
                    return; // let default behavior for space/enter when no buffer
                }

                // Backspace — remove from buffer first
                if (key === 'Backspace') {
                    if (state.buffer.length > 0) {
                        e.preventDefault();
                        // Remove the last preview characters and buffer char
                        const prevLen = _getPreviewLength(state.buffer);
                        state.buffer = state.buffer.slice(0, -1);
                        // Remove old preview, insert new preview
                        _removeChars(el, prevLen);
                        if (state.buffer) {
                            const newPreview = transliterateWord(state.buffer);
                            _insertAtCursor(el, newPreview);
                        }
                        return;
                    }
                    return; // normal backspace
                }

                // Single letter key → add to buffer
                if (key.length === 1 && /[a-zA-Z]/.test(key)) {
                    e.preventDefault();
                    // Remove old preview
                    if (state.buffer) {
                        const oldPreviewLen = _getPreviewLength(state.buffer);
                        _removeChars(el, oldPreviewLen);
                    }
                    state.buffer += key;
                    // Insert new preview (live transliteration)
                    const preview = transliterateWord(state.buffer);
                    _insertAtCursor(el, preview);
                    return;
                }
            });

            // Also flush buffer on blur
            el.addEventListener('blur', () => {
                if (state.buffer && fields[inputId]?.enabled) {
                    const tamil = transliterateWord(state.buffer);
                    // Buffer is already previewed, so no action needed besides clearing buffer
                    state.buffer = '';
                }
            });

            state.initialized = true;
        }

        state.enabled = true;
        _updateToggleBtn(inputId, true);
    }

    function _getPreviewLength(buffer) {
        return transliterateWord(buffer).length;
    }

    function _insertAtCursor(el, text) {
        const start = el.selectionStart;
        const end = el.selectionEnd;
        const val = el.value;
        el.value = val.substring(0, start) + text + val.substring(end);
        el.selectionStart = el.selectionEnd = start + text.length;
        // Trigger input event for any listeners
        el.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function _removeChars(el, count) {
        const pos = el.selectionStart;
        const val = el.value;
        if (pos >= count) {
            el.value = val.substring(0, pos - count) + val.substring(pos);
            el.selectionStart = el.selectionEnd = pos - count;
        }
    }

    function disable(inputId) {
        const state = _getState(inputId);
        // Flush any remaining buffer
        if (state.buffer) {
            state.buffer = '';
        }
        state.enabled = false;
        _updateToggleBtn(inputId, false);
    }

    function toggle(inputId) {
        const state = _getState(inputId);
        if (state.enabled) disable(inputId);
        else enable(inputId);
    }

    function isEnabled(inputId) {
        return !!fields[inputId]?.enabled;
    }

    function _updateToggleBtn(inputId, active) {
        const btn = document.getElementById(inputId + '-translit-toggle');
        if (btn) {
            btn.textContent = active ? '⌨️ தமிழ் ON' : '⌨️ ENG';
            btn.style.background = active ? '#e8f5e9' : '#f5f5f5';
            btn.style.color = active ? '#2e7d32' : '#666';
            btn.style.fontWeight = active ? '600' : '400';
            btn.title = active
                ? 'Transliteration ON – type English, get Tamil (press Space to confirm each word)'
                : 'Transliteration OFF – type normally';
        }
    }

    return { transliterateWord, enable, disable, toggle, isEnabled };
})();
