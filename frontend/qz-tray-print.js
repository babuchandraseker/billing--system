/**
 * QZ Tray silent raw ESC/POS printing (optional).
 * Requires QZ Tray 2.x installed: https://qz.io/download/
 */
(function (global) {
    'use strict';

    const QZ_CDN = 'https://cdn.jsdelivr.net/npm/qz-tray@2.2.4/qz-tray.js';

    let _qzReady = null;

    function loadScript(src) {
        return new Promise((resolve, reject) => {
            if (global.qz) return resolve(global.qz);
            const s = document.createElement('script');
            s.src = src;
            s.onload = () => resolve(global.qz);
            s.onerror = () => reject(new Error('Failed to load QZ Tray library'));
            document.head.appendChild(s);
        });
    }

    async function ensureQZ() {
        if (_qzReady) return _qzReady;
        _qzReady = (async () => {
            const qz = await loadScript(QZ_CDN);
            if (!qz.websocket.isActive()) {
                await qz.websocket.connect();
            }
            return qz;
        })();
        return _qzReady;
    }

    function base64ToUint8Array(b64) {
        const binary = atob(b64);
        const len = binary.length;
        const bytes = new Uint8Array(len);
        for (let i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
        return bytes;
    }

    async function printRawEscpos(rawBase64, printerName) {
        const qz = await ensureQZ();
        const cfg = qz.configs.create(printerName || undefined);
        const data = [{
            type: 'raw',
            format: 'command',
            data: base64ToUint8Array(rawBase64),
        }];
        await qz.print(cfg, data);
    }

    async function listPrinters() {
        const qz = await ensureQZ();
        return qz.printers.find();
    }

    global.QZTrayPrint = {
        ensureQZ,
        printRawEscpos,
        listPrinters,
    };
})(typeof window !== 'undefined' ? window : globalThis);
