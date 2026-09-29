/* ═══════════════════════════════════════════════════════
   DHANA DHANYA KADAI — Billing System  |  script.js
═══════════════════════════════════════════════════════ */

const API   = 'http://127.0.0.1:5000';
// ── Auth interceptor: redirect to login on 401 ──────────────────────────────
const _origFetch = window.fetch;
window.fetch = async function(...args) {
    const resp = await _origFetch(...args);
    if (resp.status === 401 && !String(args[0]).includes('/login')) {
        window.location.href = '/login';
    }
    return resp;
};

const API_BASE = "http://127.0.0.1:5000";
// Single source of truth for store identity — used by receipt, WhatsApp, and shortcut bar
const SHOP  = { name: 'DHANA DHANYA KADAI', sub: 'Birds Feed Factory', sub_ta: 'பறவை தீவன தொழிற்சாலை', phone: '+91 99401 16970', phone2: '+91 99443 08925' };

// ── State ─────────────────────────────────────────────
let products     = [];
let pricingMode  = 's_rate';   // 's_rate' | 'p_rate' | 'wholesale'
let packages     = [];   // [{id, name_english, name_tamil, name_tanglish, emoji, items:[...], price_per_kg}]
let cart         = [];   // [{product, weight_g, price, temp_price_per_kg}] OR [{isPackage, pkg, qty_kg, activeItems, price}]
let activeCat    = 'All';
let modalProduct = null;
let pkgModal     = null;   // currently open package config state
let billLang     = 'en';
let currentBill  = null;  // last saved bill – used by WhatsApp send
let applyLoyaltyReward = false;  // whether to apply loyalty reward on next save
let _loyaltySettings   = { enabled: true, stars_required: 10, discount: 100, min_purchase: 500 }; // cached, updated on load


const STEP_G     = 50;

// ════════════════════════════════════════════════════
// KEYBOARD SHORTCUT CONFIG  (v-patch)
// ════════════════════════════════════════════════════

const _DEFAULT_SHORTCUTS = {
    newBill:        'ctrl+n',
    exit:           'escape',
    addProduct:     'alt+p',
    showHelp:       'f8',           // moved from f1 → f8
    focusProducts:  'f1',           // F1 = focus product search (productSearch input)
    focusFirstCard: 'f2',           // F2 = focus first product card in grid
    printBill:      'f3',
    clearCart:      'f4',
    customerSearch: 'f6',           // F6 → focuses customer-phone (Enter → customer-name)
    billingPage:    'ctrl+b',
    adminPage:      'ctrl+a',
    whatsApp:       'ctrl+w',
    repeatBill:     'r',
    langToggle:     'ctrl+l',       // Ctrl+L → toggle EN / தமிழ்
    payCredit:      'ctrl+c',       // Ctrl+C → Credit mode
};

const _SHORTCUT_LABELS = {
    newBill:        'New Bill',
    exit:           'Exit',
    addProduct:     'Add Product',
    showHelp:       'Help',
    focusProducts:  'Search',
    focusFirstCard: 'Products',
    printBill:      'Print Bill',
    clearCart:      'Clear Cart',
    customerSearch: 'Customer',
    billingPage:    'Billing',
    adminPage:      'Admin',
    whatsApp:       'WhatsApp',
    repeatBill:     'Repeat Bill',
    langToggle:     'Language',
    payCredit:      'Credit Mode',
};

let _shortcuts = {};

async function _loadShortcuts() {
    try {
        // FIX: Load from server (persists across restarts) with localStorage as fast-cache fallback
        const resp = await fetch(`${API}/api/shortcuts`);
        if (resp.ok) {
            const d = await resp.json();
            if (d.success && d.shortcuts && Object.keys(d.shortcuts).length > 0) {
                const valid = { ..._DEFAULT_SHORTCUTS };
                for (const k in valid) {
                    if (d.shortcuts[k]) valid[k] = d.shortcuts[k];
                }
                _shortcuts = valid;
                delete _shortcuts.payUPI;
                delete _shortcuts.payCash;
                localStorage.setItem('kb_shortcuts', JSON.stringify(_shortcuts)); // sync local cache
                _renderShortcutBar();
                return;
            }
        }
    } catch (e) { /* server not ready yet — fall through to localStorage */ }
    // Fast local cache fallback
    try {
        const saved = JSON.parse(localStorage.getItem('kb_shortcuts') || 'null');
        const valid = { ..._DEFAULT_SHORTCUTS };
        if (saved) {
            for (const k in valid) {
                if (saved[k]) valid[k] = saved[k];
            }
        }
        _shortcuts = valid;
    } catch { _shortcuts = { ..._DEFAULT_SHORTCUTS }; }
    delete _shortcuts.payUPI;
    delete _shortcuts.payCash;
    _renderShortcutBar();
}

async function _saveShortcuts(map) {
    _shortcuts = { ...map };
    localStorage.setItem('kb_shortcuts', JSON.stringify(map)); // instant local update
    _renderShortcutBar();
    try {
        // FIX: Persist to server DB so shortcuts survive app restarts
        await fetch(`${API}/api/shortcuts`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ shortcuts: map })
        });
    } catch (e) { console.warn('[shortcuts] server save failed, kept in localStorage:', e); }
}

// Normalise a KeyboardEvent → "ctrl+s", "f3", "alt+p", "escape"
function _eventToKey(e) {
    const parts = [];
    if (e.ctrlKey || e.metaKey) parts.push('ctrl');
    if (e.altKey)               parts.push('alt');
    if (e.shiftKey)             parts.push('shift');
    const k = e.key.toLowerCase();
    if (!['control','alt','shift','meta'].includes(k)) parts.push(k);
    return parts.join('+');
}

function _getShortcutHandlers() {
    return {
        // ── New Bill: fully resets cart + customer + payment + focus ──────────
        newBill: () => {
            if (cart.length && !_lastSavedBill &&
                !confirm('This cart has NOT been saved as a bill.\nDiscard it and start a new bill?')) return;
            resetBillingState();
            resetFocus();
            showToast('🆕 New Bill');
        },

        exit: () => {
            closeWeightModal(); closeEditModal(); closeBillDetail();
            closeCustHistory?.(); closePackageModal?.();
        },

        addProduct: () => {
            setTab('admin');
            switchAdminTab('products');
            setTimeout(() => { document.getElementById('adm-name-en')?.focus(); }, 80);
        },

        // ── F2: Jump focus to first product card in grid ─────────────────────
        focusFirstCard: () => {
            const first = document.querySelector('#products-grid .pcard');
            if (first) { first.focus(); _highlightCard(0); }
        },

        // ── F1: Help overlay ──────────────────────────────────────────────────
        showHelp: () => _showKeyboardHelp(),

        // ── F2: Jump focus to product search + clear highlight ────────────────
        focusProducts: () => focusProductSection(),

        // ── F3: Print ─────────────────────────────────────────────────────────
        printBill: () => handlePrint(),

        // ── F4: Clear cart ────────────────────────────────────────────────────
        clearCart: () => clearCart(),

        // ── F6: Customer name (auto-expands collapsible section) ──────────────
        customerSearch: () => focusCustomer(),


        repeatBill: () => {
            if (!window.lastBill) { showToast('⚠️ No previous bill to repeat'); return; }
            cart = JSON.parse(JSON.stringify(window.lastBill.cart));
            currentBill = null;
            renderCart();
            showToast('🔁 Repeat Bill loaded');
        },
        billingPage:  () => setTab('billing'),
        adminPage:    () => { setTab('admin'); switchAdminTab(_lastAdminTab || 'products'); },
        whatsApp: () => {
            if (!cart || !cart.length) { showToast('⚠️ Cart is empty — nothing to share'); return; }
            sendBillToWhatsApp(null);
        },
        // ── New shortcuts ──────────────────────────────────────────────────────
        langToggle: () => setBillLang(billLang === 'en' ? 'ta' : 'en'),
        payCredit:  () => { if (cart.length) setPaymentMethod('credit'); else showToast('⚠️ Cart is empty'); },
    };
}

// Dynamic shortcut bar renderer — populates top bar AND sticky bottom bar
function _renderShortcutBar() {
    const allKeys = ['newBill', 'focusProducts', 'focusFirstCard', 'printBill', 'customerSearch', 'repeatBill', 'clearCart', 'whatsApp', 'showHelp', 'addProduct', 'billingPage', 'adminPage'];
    const items = allKeys
        .filter(action => _shortcuts[action])
        .map(action => {
            const key = _shortcuts[action];
            const label = _SHORTCUT_LABELS[action] || action;
            const keyDisplay = key
                .replace('ctrl+',  'Ctrl+')
                .replace('alt+',   'Alt+')
                .replace('shift+', 'Shift+')
                .replace(/^f(\d+)$/, 'F$1')
                .replace('escape',  'Esc');
            return `<span class="sc-item"><kbd>${keyDisplay}</kbd><span class="sc-label">${label}</span></span>`;
        });
    const html = items.join('');
    const top    = document.getElementById('sc-dynamic-group');
    const bottom = document.getElementById('sc-dynamic-group-bottom');
    if (top)    top.innerHTML    = html;
    if (bottom) bottom.innerHTML = html;

    setTimeout(() => {
        const bar = document.querySelector('.shortcut-bar');
        if (bar) {
            const h = bar.offsetHeight || bar.scrollHeight;
            if (h > 20) {
                document.documentElement.style.setProperty('--scbar', h + 'px');
            }
        }
    }, 50);
}
// ── Global Currency Formatter ─────────────────────────
function formatCurrency(amount) {
  return '<span class="amount"><span class="currency">\u20b9</span> <span class="value">' + parseFloat(amount).toFixed(2) + '</span></span>';
}


// ── Lazy-load / infinite-scroll state ────────────────
const PAGE_SIZE      = 50;     // products per page
let   _lazyList      = [];     // current filtered+sorted product list
let   _lazyRendered  = 0;      // how many are currently in the DOM
let   _lazyLoading   = false;  // guard: prevent concurrent appends
let   _lazyObserver  = null;   // IntersectionObserver instance

// ── Tamil / Tanglish keyword map ──────────────────────
// ── Search synonym map: typed word → alternative words to also match ──
// Works in all directions: English alias, Tamil word, Tanglish phonetic
const SEARCH_SYNONYMS = {
  // English aliases
  'peanut':['groundnut','kadalai','katalai'],
  'bajra':['kambu','pearl millet'],
  'jowar':['solam','sorghum'],
  'ragi':['finger millet','keezhvaraku','kelvaragu','kezhvaragu'],
  'dhal':['dal','paruppu'],
  'dal':['paruppu','lentil'],
  'sesame':['til','ellu','gingelly'],
  'gingelly':['ellu','sesame'],
  'mustard':['kadugu','rai'],
  'turmeric':['manjal','haldi'],
  'pepper':['milagu','black pepper'],
  'cumin':['seeragam','jeera'],
  'jeera':['cumin','seeragam'],
  'fenugreek':['methi','venthayam'],
  'methi':['fenugreek','venthayam'],
  'ajwain':['omam','carom'],
  'carom':['omam','ajwain'],
  'flax':['alsi','ali virai'],
  'hemp':['hemp seeds'],
  'chia':['sabja'],
  'sabja':['sweet basil','chia'],
  'moong':['green gram','payatham'],
  'urad':['black gram','ulundhu'],
  'ulundhu':['urad','black gram'],
  'toor':['red gram','thuvaram'],
  'chana':['chickpea','kadalai'],
  'rajma':['kidney beans','rajma'],
  'horse gram':['kollu'],
  'kollu':['horse gram'],
  'corn':['maize','makka','cholam'],
  'maize':['corn','makka'],
  'wheat':['gothumai','atta'],
  'gothumai':['wheat'],
  'barley':['barli'],
  'oats':['oatmeal','ots'],
  'rice':['arisi'],
  'arisi':['rice'],
  'honey':['then','madhu'],
  'dates':['pericham','kharjur'],
  'almond':['badam','vadumai'],
  'badam':['almond'],
  'cashew':['munthiri','kaju'],
  'walnut':['akrut','akhrot'],
  'pistachio':['pista'],
  'groundnut':['peanut','kadalai','katalai'],
  'kadalai':['groundnut','peanut'],
  'sunflower':['sooriyakanthi','suryakanthi'],
  'safflower':['sappola','kusumba'],
  'sappola':['safflower'],
  'niger':['ramtil','karuppu ellu'],
  'flaxseed':['alsi','ali virai','flax'],
  'pumpkin':['poosanikai'],
  'watermelon':['tharbusani'],
  'millet':['thinai','varagu','kambu','samai','ragi','kuthiraivali'],
  'varagu':['kodo','varaku'],
  'kodo':['varagu','varaku'],
  'thinai':['foxtail'],
  'samai':['little millet','chaamai'],
  'kambu':['pearl millet','bajra','kampu'],
  'kampu':['kambu','pearl millet'],
  'sorghum':['solam','cholam','jowar'],
  'solam':['sorghum'],
};

// ════════════════════════════════════════════════════
// PRODUCT IMAGE RESOLVER
// ════════════════════════════════════════════════════
//
// Resolution order for every product card:
//   1. product.image  — exact filename from the database (e.g. "RAGI.webp")
//      Populated for all 108+ products that have a matching webp on disk,
//      plus every image uploaded through the admin panel.
//   2. WEBP_NAME_MAP  — name-to-filename lookup, fallback for DB-empty products.
//   3. images/default.webp — always shown when nothing else resolves.
//
// The frontend never guesses from product.name; the DB image field is the source of truth.
// To add an image to a product: go Admin → edit product → upload image.
// ════════════════════════════════════════════════════

/**
 * Primary image resolver used by every product card.
 * Returns a path like "images/RAGI.webp" or "images/default.webp".
 *   product.image  → exact filename from DB  (e.g. "RAGI.webp")
 *   null / ""      → "images/default.webp"
 */
function getProductImage(product) {
    if (product && product.image) { return 'images/' + product.image; }
    // Fallback: try to resolve from product name
    const nameEn = product && (product.name_english || product.name_en || product.name || '');
    if (nameEn) { return getProductImagePath(nameEn, ''); }
    return 'images/default.webp';
}

/**
 * WEBP_NAME_MAP — used only as a fallback when product.image is not set.
 * Maps uppercase English product name → exact webp filename on disk.
 */
const WEBP_NAME_MAP = {
    // ── Millets ────────────────────────────────────────────────────────────────
    'FOXTAIL MILLET':                    'foxtail_seeds.webp',
    'FOXTAIL SEEDS':                     'foxtail_seeds.webp',
    'RED FOXTAIL MILLET':                'red_foxtail_seeds.webp',
    'RED FOXTAIL SEEDS':                 'red_foxtail_seeds.webp',
    'BARNYARD MILLET':                   'barnyard_millet.webp',
    'LITTLE MILLET':                     'samai.webp',
    'SAMAI':                             'samai.webp',
    'PEARL MILLET':                      'pearl_millet.webp',
    'KODO MILLET':                       'kodo_millet.webp',
    'KODO MILLET RICE':                  'kodo_millet_rice.webp',
    'VARAGU MILLET':                     'pani_varagu.webp',
    'PANI VARAGU':                       'pani_varagu.webp',
    'VARAGU MILLET RICE':                'varagu_millet_rice.webp',
    'BROWNTOP MILLET':                   'finger_millet.webp',
    'FINGER MILLET':                     'finger_millet.webp',
    'FINGER MILLET (RAGI)':              'ragi.webp',
    'RAGI':                              'ragi.webp',
    'PEARL MILLET (NATIVE)':             'pearl_millet.webp',
    'FOXTAIL MILLET (GRADE 2)':          'foxtail_seeds.webp',
    'FOX MILLET':                        'fox_millet.webp',
    'BLACK MILLET':                      'black_millet.webp',
    'GREEN MILLET':                      'green_millet.webp',
    'RED MILLET':                        'red_millet.webp',
    'WHITE MILLET':                      'white_millet.webp',
    'YELLOW MILLET':                     'yellow_millet.webp',
    'WHITE SORGHUM':                     'white_sorghum.webp',
    'RED SORGHUM':                       'red_sorghum.webp',
    'DARK SORGHUM':                      'dark_sorghum.webp',
    // ── Rice & Grains ──────────────────────────────────────────────────────────
    'RED RICE':                          'red_rice.webp',
    'BLACK RICE':                        'black_rice.webp',
    'KARUNGURUVAI BLACK RICE':           'karunguruvai_black_rice.webp',
    'MAPPILLAI SAMBA RICE':              'mappillai_samba_rice.webp',
    'BAMBOO RICE':                       'bamboo_rice.webp',
    'RICE':                              'rice.webp',
    'PADDY (RAW RICE)':                  'paddy.webp',
    'PADDY':                             'paddy.webp',
    'BUCKWHEAT':                         'buckwheat.webp',
    'WHEAT':                             'wheat.webp',
    'SAMBA WHEAT':                       'samba_wheat.webp',
    'OATS':                              'long_oats.webp',
    'LONG OATS':                         'long_oats.webp',
    'ROLLED OATS':                       'peeled_oats.webp',
    'PEELED OATS':                       'peeled_oats.webp',
    'QUINOA':                            'quinoa.webp',
    'BARLEY':                            'barley.webp',
    'BARLEY HUSK':                       'barley_husk.webp',
    'POPCORN':                           'popcorn.webp',
    'SWEET CORN':                        'sweet_corn.webp',
    'DRIED FIELD CORN':                  'dried_field_corn.webp',
    // ── Seeds ──────────────────────────────────────────────────────────────────
    'SUNFLOWER SEEDS':                   'sunflower_seeds.webp',
    'BIG SUNFLOWER SEEDS':               'big_sunflower_seeds.webp',
    'PEALED SUNFLOWER SEED':             'pealed_sunflower_seed.webp',
    'BLACK SESAME SEEDS':                'black_sesame_seeds.webp',
    'WHITE SESAME SEEDS':                'white_sesame_seeds.webp',
    'CHIA SEEDS':                        'chia_seeds.webp',
    'CUCUMBER SEEDS':                    'cucumber_seeds.webp',
    'PUMPKIN SEEDS':                     'pumpkin_seeds.webp',
    'POPPY SEEDS':                       'poppy_seeds.webp',
    'MUSTARD SEEDS':                     'mustard_seeds.webp',
    'FENUGREEK SEEDS':                   'fenugreek_seeds.webp',
    'MORINGA SEEDS':                     'moringa_seeds.webp',
    'NIGER SEEDS':                       'niger_seeds.webp',
    'ALFALFA SEEDS':                     'alfalfa_seeds.webp',
    'CANARY SEEDS':                      'canary_seeds.webp',
    'DILL SEEDS':                        'dill_seeds.webp',
    'FLAX SEEDS':                        'flax_seeds.webp',
    'MILK THISTLE SEEDS':                'milk_thistle_seeds.webp',
    // ── Pulses / Protein ───────────────────────────────────────────────────────
    'GREEN GRAM (MOONG)':                'green_gram.webp',
    'GREEN GRAM':                        'green_gram.webp',
    'HORSE GRAM':                        'horse_gram.webp',
    'BLACK URAD DAL':                    'black_urad.webp',
    'BLACK URAD':                        'black_urad.webp',
    'RED LENTIL (MASOOR DAL)':           'red_lentil.webp',
    'RED LENTIL':                        'red_lentil.webp',
    'SOYBEAN':                           'soya.webp',
    'SOYA':                              'soya.webp',
    'FIELD BEANS':                       'field_beans.webp',
    'RED PIGEON PEA':                    'red_pigeon_pea.webp',
    'RED COW PEA':                       'red_cowpea.webp',
    'WHITE PIGEON PEA':                  'white_pigeon_pea.webp',
    'KIDNEY BEANS (RAJMA)':              'kidney_beans.webp',
    'KIDNEY BEANS':                      'kidney_beans.webp',
    'CHICKPEAS':                         'chickpeas.webp',
    'WHITE CHICKPEAS':                   'white_chickpeas.webp',
    'WHITE PEAS':                        'white_peas.webp',
    'GREEN PEAS':                        'green_peas.webp',
    // ── Nuts ───────────────────────────────────────────────────────────────────
    'ALMONDS':                           'almond.webp',
    'ALMOND':                            'almond.webp',
    'CASHEW':                            'cashew_nut.webp',
    'CASHEW NUT':                        'cashew_nut.webp',
    'WALNUT':                            'walnut.webp',
    'PISTACHIO':                         'pistachio.webp',
    'GOLDEN GROUNDNUT':                  'golden_groundnut.webp',
    'GROUNDNUT (PEANUT)':                'groundnut.webp',
    'GROUNDNUT':                         'groundnut.webp',
    'CHAROLI':                           'charoli.webp',
    // ── Spices ─────────────────────────────────────────────────────────────────
    'CUMIN SEEDS':                       'cumin.webp',
    'CUMIN':                             'cumin.webp',
    'BLACK CUMIN (NIGELLA / KALONJI)':   'black_cumin.webp',
    'BLACK CUMIN':                       'black_cumin.webp',
    'MUSTARD':                           'mustard_seeds.webp',
    'FENUGREEK':                         'fenugreek_seeds.webp',
    // ── Supplements / Other ────────────────────────────────────────────────────
    'HONEY':                             'honey.webp',
    'RAISINS':                           'raisin.webp',
    'RAISIN':                            'raisin.webp',
    'ALMOND GUM (BADAM PISIN)':          'almond_gum.webp',
    'ALMOND GUM':                        'almond_gum.webp',
    // ── Other named products on disk ───────────────────────────────────────────
    'SALIYAL':                           'saliyal.webp',
    'SALAIYAL':                          'saliyal.webp',
    'TURMERIC POWDER':                   'turmeric_podwer.webp',
    'TURMERIC PODWER':                   'turmeric_podwer.webp',
    'NAMAKKARAMANI':                     'namakkaramani.webp',
    'THAM POWDER':                       'tham_powder.webp',
    'UTCHA KADALI':                      'utcha_kadali.webp',
    'EGG SHELL':                         'egg_shell.webp',
    'EGGSHELL':                          'egg_shell.webp',
    'EGGSHELL POWDER':                   'egg_shell.webp',
    'EMBU':                              'embu.webp',
    'EMBU SEEDS':                        'embu_seeds.webp',
    'GRET':                              'gret.webp',
    'KEERAI':                            'keerai.webp',
    'KATTU YANAM':                       'kattu_yanam.webp',
    'LIMESTONE':                         'limestone.webp',
    'LIME STONE':                        'limestone.webp',
    'LIME STONE POWDER':                 'limestone.webp',
    'LIME STONE POWDER 250G':            'limestone.webp',
    'HAMSTER PELLETS':                   'hamster_pellets.webp',
    'HAMSTER PELLET':                    'hamster_pellets.webp',
    'HAMSTER':                           'hamster_pellets.webp',
    'RABBIT PELLETS':                    'rabbit_pellets.webp',
    'RABBIT PELLET':                     'rabbit_pellets.webp',
    'RED COWPEA':                        'red_cowpea.webp',
    'RED COW PEA':                       'red_cowpea.webp',
    'SAPOLA':                            'SAPOLA_SEEDS.webp',
    'SAPOLA SEEDS':                      'SAPOLA_SEEDS.webp',
    'SAFFLOWER SEEDS':                   'SAPOLA_SEEDS.webp',
    'STARTER (SKM)':                     'STARTER(SKM).webp',
    'STARTER(SKM)':                      'STARTER(SKM).webp',
    'STARTER (SKM FEED)':                'STARTER(SKM).webp',
    'STARTER FEED 5KG':                  'STARTER(SKM).webp',
    'KADAMOZHU PACKET 500G':             'KADAMOZHU PACKET 500g.webp',
    'KADAMOZHU':                         'KADAMOZHU PACKET 500g.webp',
};

/**
 * getProductImagePath — fallback resolver for products where product.image is not set.
 * Used by _buildProductCard; always returns a valid path (never null).
 */

// ── PRICING MODE ──────────────────────────────────────────────────────────────
let _pendingPriceMode = null;
let _priceModeInitialized = false;

function setPricingMode(mode) {
    // Show confirmation only when user clicks (not on initial load)
    if (_priceModeInitialized && mode !== pricingMode) {
        _pendingPriceMode = mode;
        const labels = { 's_rate': 'S.Rate (Selling)', 'p_rate': 'P.Rate (Purchase)', 'wholesale': 'Wholesale' };
        document.getElementById('price-mode-target').textContent = labels[mode] || mode;
        document.getElementById('price-mode-overlay').classList.add('open');
        document.getElementById('price-mode-modal').classList.add('open');
        return;
    }
    _applyPricingMode(mode);
    _priceModeInitialized = true;
}

function _applyPricingMode(mode) {
    pricingMode = mode;
    ['s_rate','p_rate','wholesale'].forEach(m => {
        const id = 'pm-' + m.replace('_','');
        const btn = document.getElementById(id);
        if (btn) btn.classList.toggle('pm-active', m === mode);
    });
    products = products.map(p => ({...p, price_per_kg: getEffectivePrice(p)}));
    renderProducts();
    syncCartPricesWithPricingMode();
}

/** Per-kg rate for a cart line (respects temp override; else current pricing mode). */
function _getCartItemPricePerKg(item) {
    if (!item || item.isPackage) return 0;
    if (item.temp_price_per_kg != null) return item.temp_price_per_kg;
    const fresh = products.find(p => p.id === item.product?.id) || item.product;
    return getEffectivePrice(fresh);
}

/** Recalculate all cart line totals when S.Rate / P.Rate / Wholesale changes. */
function syncCartPricesWithPricingMode() {
    if (!cart.length) return;
    window.currentBill = null;
    cart.forEach(item => {
        if (item.isPackage) {
            const freshPkg = packages.find(p => p.id === item.pkg?.id);
            if (freshPkg) item.pkg = freshPkg;
            const pkgRate = freshPkg ? (freshPkg.price_per_kg || 0) : (item.pkg?.price_per_kg || 0);
            item.price = calcPackagePrice(item.qty_kg || 0, pkgRate);
            return;
        }
        if (!item.product) return;
        const fresh = products.find(p => p.id === item.product.id);
        if (fresh) item.product = fresh;
        const perKg = _getCartItemPricePerKg(item);
        item.price = calcPrice(perKg, item.weight_g);
    });
    renderCart();
    updateTotals();
}

function confirmPriceModeChange() {
    document.getElementById('price-mode-overlay').classList.remove('open');
    document.getElementById('price-mode-modal').classList.remove('open');
    if (_pendingPriceMode) {
        _applyPricingMode(_pendingPriceMode);
        showToast(`✅ Price mode changed to ${getPriceModeLabel()}`);
        _pendingPriceMode = null;
    }
}

function cancelPriceModeChange() {
    document.getElementById('price-mode-overlay').classList.remove('open');
    document.getElementById('price-mode-modal').classList.remove('open');
    _pendingPriceMode = null;
}

function getEffectivePrice(p) {
    if (pricingMode === 'p_rate')    return p.p_rate != null    ? p.p_rate    : (p.price_per_kg || 100);
    if (pricingMode === 'wholesale') return p.wholesale_rate != null ? p.wholesale_rate : (p.s_rate || p.price_per_kg || 100);
    return p.s_rate != null ? p.s_rate : (p.price_per_kg || 100);  // s_rate default
}

function getPriceModeLabel() {
    if (pricingMode === 'p_rate')    return 'P.Rate';
    if (pricingMode === 'wholesale') return 'Wholesale';
    return 'S.Rate';
}

// ── DYNAMIC CATEGORY PILLS ────────────────────────────────────────────────────
const CAT_EMOJI = {
    'Millets':'🌾','Rice Varieties':'🍚','Oats & Cereals':'🌿','Corn & Maize':'🌽',
    'Pulses & Legumes':'🫘','Sorghum & Grain':'🌱','Groundnuts':'🥜',
    'Nuts & Dry Fruits':'🌰','Seeds':'🌻','Spices & Condiments':'🧂',
    'Ayurvedic Herbs & Roots':'💊','Bird Feed Mixes':'🐦','Pet & Animal Feed':'🐾',
    'Minerals & Grit':'💎','Honey & Natural Sweeteners':'🍯',
    'Processed Products':'📦','Miscellaneous / Internal':'🗂️',
    'Supplements':'💊',
};

function buildCategoryPills() {
    const cats = [...new Set(products.map(p => p.category).filter(Boolean))].sort();
    const container = document.getElementById('cat-pills');
    if (!container) return;
    const activeCat = container.querySelector('.cpill.active') ? container.querySelector('.cpill.active').dataset.cat : 'All';
    container.innerHTML = '';
    const allBtn = document.createElement('button');
    allBtn.className = 'cpill' + (activeCat === 'All' ? ' active' : '');
    allBtn.setAttribute('data-cat','All');
    allBtn.onclick = function(){ filterCat('All', this); };
    allBtn.textContent = 'All';
    container.appendChild(allBtn);
    cats.forEach(cat => {
        const btn = document.createElement('button');
        btn.className = 'cpill' + (activeCat === cat ? ' active' : '');
        btn.setAttribute('data-cat', cat);
        btn.onclick = function(){ filterCat(cat, this); };
        btn.textContent = (CAT_EMOJI[cat] || '📦') + ' ' + cat;
        container.appendChild(btn);
    });
}

function _normalizeToFilename(name) {
    // Normalize product name to match image filename format:
    // lowercase, remove brackets & special chars, spaces→underscore
    let s = name.toLowerCase();
    s = s.replace(/\([^)]*\)/g, '');       // remove (bracketed) text
    s = s.replace(/[^a-z0-9\s]/g, '');      // keep only alnum + spaces
    s = s.trim().replace(/\s+/g, '_');       // spaces → underscore
    return s;
}

function getProductImagePath(nameEn, imgField) {
    // If DB field is set, use it directly — no guessing needed
    if (imgField) { return 'images/' + imgField; }

    // Name-map fallback for products not yet in the DB
    if (nameEn) {
        const up     = nameEn.trim().toUpperCase();
        const before = up.split('(')[0].trim();
        if (WEBP_NAME_MAP[up])     { return 'images/' + WEBP_NAME_MAP[up]; }
        if (WEBP_NAME_MAP[before]) { return 'images/' + WEBP_NAME_MAP[before]; }

        // Smart normalize fallback: try lowercase_underscored.webp
        const normalized = _normalizeToFilename(nameEn);
        if (normalized) { return 'images/' + normalized + '.webp'; }
    }

    return 'images/default.webp';
}

// ════════════════════════════════════════════════════
// INIT
// ════════════════════════════════════════════════════
// ════════════════════════════════════════════════════
// OFFLINE DETECTION + AUTO-RETRY
// ════════════════════════════════════════════════════
let _retryTimer = null;

function showOfflineBanner() {
    document.getElementById('offline-banner').style.display = 'flex';
    document.getElementById('products-grid').innerHTML = '';
    // Auto-retry every 3 seconds
    clearInterval(_retryTimer);
    _retryTimer = setInterval(async () => {
        try {
            const r = await fetch(`${API}/health`, {signal: AbortSignal.timeout(2000)});
            if (r.ok) {
                clearInterval(_retryTimer);
                fetchProducts();
            }
        } catch {}
    }, 3000);
}

// ── Focus helper — returns cursor to search after billing actions ─────────
function focusSearch() {
    const s = document.getElementById('productSearch');
    if (s) { s.value = ''; s.focus(); }
}

// ════════════════════════════════════════════════════
// GLOBAL FOCUS CONTROLLER  (v5-patch)
// All keyboard focus transitions route through these
// functions — never call .focus() inline elsewhere.
// ════════════════════════════════════════════════════

/** Move focus to the product search field and clear any card highlight. */
function focusProductSection() {
    if (typeof _clearHighlight === 'function') _clearHighlight();
    const s = document.getElementById('productSearch');
    if (s) { s.focus(); s.select(); }
}

/**
 * Expand customer info section (if collapsed) and focus the PHONE field.
 * F6 triggers this. Enter on phone moves to name (see DOMContentLoaded handler).
 * The 40 ms delay lets the CSS flex transition complete before focus lands.
 */
function focusCustomer() {
    const el    = document.getElementById('customer-inputs-collapsible');
    const arrow = document.getElementById('customer-toggle-arrow');
    if (el && el.style.display === 'none') {
        el.style.display = 'flex';
        if (arrow) arrow.style.transform = 'rotate(180deg)';
    }
    setTimeout(() => {
        const phoneEl = document.getElementById('customer-phone');
        if (phoneEl) { phoneEl.focus(); phoneEl.select(); }
    }, 40);
}

/** Move focus to the cash-received input in the bill footer. */
function focusPayment() {
    if (!cart.length) { showToast('⚠️ Add items to bill first'); return; }
    const el = document.getElementById('cash-input');
    if (el) { el.focus(); el.select(); }
}

/** ESC-level reset: clear card highlight, return focus to product search. */
function resetFocus() {
    if (typeof _clearHighlight === 'function') _clearHighlight();
    focusProductSection();
}

// ── Keyboard Help Overlay ─────────────────────────────────────────────────────
function _showKeyboardHelp() {
    document.getElementById('kb-help-overlay')?.remove();

    // AUDIT FIX: generated from the ACTIVE shortcut map (the old hardcoded list
    // advertised F7/F9–F12/Ctrl+S which did nothing, and ignored the saved keys).
    const _fmtKey = k => String(k).replace('ctrl+', 'Ctrl+').replace('alt+', 'Alt+')
        .replace('shift+', 'Shift+').replace(/^f(\d+)$/, 'F$1').replace('escape', 'Esc')
        .replace(/^([a-z])$/, m => m.toUpperCase());
    const shortcuts = Object.entries(_shortcuts)
        .filter(([action, k]) => k && action !== 'exit')
        .map(([action, k]) => [_fmtKey(k), _SHORTCUT_LABELS[action] || action]);
    shortcuts.push(
        ['Z',        'New bill (when no text field is focused)'],
        ['Ctrl+K',   'Focus product search'],
        ['↑ ↓',      'Navigate product list (Up / Down)'],
        ['← →',      'Navigate product grid (Left / Right)'],
        ['Enter',    'Add highlighted product'],
        ['Esc',      'Close modal / reset focus'],
    );

    const rows = shortcuts.map(([k, d]) =>
        `<tr>
           <td style="padding:5px 14px 5px 0;white-space:nowrap;">
             <kbd style="background:#2d1f6e;color:#c4b5fd;border:1.5px solid #7c3aed;
                         border-radius:5px;padding:2px 8px;font-size:12px;font-family:monospace;">
               ${k}
             </kbd>
           </td>
           <td style="padding:5px 0;font-size:13px;color:#d1d5db;">${d}</td>
         </tr>`
    ).join('');

    const overlay = document.createElement('div');
    overlay.id = 'kb-help-overlay';
    overlay.style.cssText = [
        'position:fixed','inset:0','background:rgba(0,0,0,0.68)',
        'z-index:99999','display:flex','align-items:center','justify-content:center',
    ].join(';');
    overlay.innerHTML = `
        <div style="background:#1e1b4b;color:#fff;padding:28px 32px;border-radius:16px;
                    max-width:460px;width:92%;box-shadow:0 12px 48px rgba(0,0,0,0.6);
                    border:1.5px solid #7c3aed;font-family:inherit;
                    max-height:90vh;overflow-y:auto;">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;">
            <h2 style="margin:0;font-size:16px;color:#a78bfa;">⌨️ Keyboard Shortcuts</h2>
            <button onclick="document.getElementById('kb-help-overlay').remove()"
              style="background:transparent;border:none;color:#a78bfa;
                     font-size:20px;cursor:pointer;line-height:1;padding:0;">✕</button>
          </div>
          <table style="border-collapse:collapse;width:100%;">${rows}</table>
          <p style="margin:16px 0 0;font-size:11px;color:#6b7280;text-align:center;">
            Press
            <kbd style="background:#2d1f6e;color:#c4b5fd;border:1.5px solid #7c3aed;
                        border-radius:4px;padding:1px 6px;font-size:11px;">Esc</kbd>
            or click outside to close
          </p>
        </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

    // Close on Esc via capture phase — fires before the main handler
    const _hk = e => {
        if (e.key === 'Escape') {
            e.stopImmediatePropagation();
            overlay.remove();
            document.removeEventListener('keydown', _hk, true);
        }
    };
    document.addEventListener('keydown', _hk, true);
}

document.addEventListener('DOMContentLoaded', () => {
    _loadShortcuts();          // FIX: async — loads from server DB (persists restarts)
    // _renderShortcutBar is called inside _loadShortcuts after data arrives
    startClock();
    loadProducts();

    // Register all modals with the global ModalManager (ESC stack)
    _registerAllModals();

    // Auto-focus product search on page load via centralized controller
    setTimeout(() => focusProductSection(), 300);

    // Expand customer info section by default so keyboard flow works without mouse
    (() => {
        const el    = document.getElementById('customer-inputs-collapsible');
        const arrow = document.getElementById('customer-toggle-arrow');
        if (el)    el.style.display = 'flex';
        if (arrow) arrow.style.transform = 'rotate(180deg)';
    })();

    // Auto-focus transition: customer-name Tab → cash-input (payment)
    // Only intercepts Tab when cart has items — lets normal Tab work otherwise
    document.getElementById('customer-name')?.addEventListener('keydown', e => {
        if (e.key === 'Tab' && !e.shiftKey && cart.length) {
            e.preventDefault();
            focusPayment();
        }
    });

    // Auto-focus transition: cash-input Enter → back to product search
    document.getElementById('cash-input')?.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            e.preventDefault();
            // Enter on cash input → focus Save button for one-key save
            const saveBtn = document.querySelector('.btn-show');
            if (saveBtn) saveBtn.focus();
            else resetFocus();
        }
        if (e.key === 'Tab' && !e.shiftKey) {
            e.preventDefault();
            const saveBtn = document.querySelector('.btn-show');
            if (saveBtn) saveBtn.focus();
        }
    });

    // paid-amount-input (credit mode): Enter → focus Save
    document.getElementById('paid-amount-input')?.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            e.preventDefault();
            updateTotals();
            const saveBtn = document.querySelector('.btn-show');
            if (saveBtn) saveBtn.focus();
        }
    });

    // Cache loyalty settings for use in bill receipts
    fetch(`${API}/api/loyalty-settings`)
        .then(r => r.json())
        .then(d => {
            const s = d.settings || d;
            _loyaltySettings = {
                enabled:        !!(s.loyalty_enabled ?? 1),
                stars_required: parseInt(s.stars_for_reward  || s.stars_required || 10),
                discount:       parseFloat(s.reward_discount || s.discount || 100),
                min_purchase:   parseFloat(s.min_purchase_for_star || s.min_purchase || 500)
            };
        })
        .catch(() => { /* keep safe defaults */ });

    // Reset name auto-fill flag when user manually edits name
    document.getElementById('customer-name').addEventListener('input', () => {
        _nameAutoFilled = false;
    });

    // Reset Tamil auto-fill flag when user manually edits Tamil name field
    document.getElementById('adm-name-ta').addEventListener('input', () => {
        _taAutoFilled = false;
    });

    // Clear Tamil when English name field is fully cleared
    document.getElementById('adm-name-en').addEventListener('input', () => {
        if (!document.getElementById('adm-name-en').value.trim()) {
            if (_taAutoFilled) {
                document.getElementById('adm-name-ta').value = '';
                _taAutoFilled = false;
            }
        }
    });

    // Customer search: press Enter key to search
    document.getElementById('cust-search-input').addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); searchCustomers(); }
    });

    // ── Task 4 & 3: Enter-based navigation for customer → payment ────────────
    // customer-phone → customer-name on Enter (F6 flow)
    document.getElementById('customer-phone')?.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            e.preventDefault();
            document.getElementById('customer-name')?.focus();
        }
    });
    // customer-name → cash-input on Enter
    document.getElementById('customer-name')?.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            e.preventDefault();
            document.getElementById('cash-input')?.focus();
        }
    });

    // ── Task 8: Admin product form Enter navigation ───────────────────────────
    const _adminEnterFields = [
        'adm-name-en', 'adm-name-ta', 'adm-srate', 'adm-prate',
        'adm-wholesale', 'adm-cat', 'adm-emoji', 'adm-tags',
        'adm-nut-protein', 'adm-nut-abs-protein', 'adm-nut-fat',
        'adm-nut-carbs', 'adm-nut-fiber', 'adm-nut-moisture', 'adm-nut-ash'
    ];
    _adminEnterFields.forEach((id, i) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('keydown', e => {
            if (e.key === 'Enter') {
                e.preventDefault();
                e.stopPropagation();
                const nextId = _adminEnterFields[i + 1];
                if (nextId) {
                    const nextEl = document.getElementById(nextId);
                    if (nextEl) { nextEl.focus(); if (nextEl.select) nextEl.select(); }
                } else {
                    // Last nutrition field → click Add Product button
                    const addBtn = document.getElementById('adm-add-btn');
                    if (addBtn) addBtn.click();
                    else adminAddProduct?.();
                }
            }
        });
    });

    // Admin page auto-focus when switching to products tab
    const _origSwitchAdminTab = window.switchAdminTab;
    window.switchAdminTab = function(tab) {
        _origSwitchAdminTab?.(tab);
        if (tab === 'products') {
            setTimeout(() => { document.getElementById('adm-name-en')?.focus(); }, 80);
        }
    };
});

function startClock() {
    const el = document.getElementById('clock');
    const tick = () => { el.textContent = new Date().toLocaleTimeString('en-IN', {hour12:false}); };
    tick(); setInterval(tick, 1000);
}

// ════════════════════════════════════════════════════
// OPEN CATALOG PDF — navbar shortcut (Alt+C)
// Opens the print/PDF version of the product catalog.
// Equivalent to running: Open_Catalog_PDF.bat
// ════════════════════════════════════════════════════
/**
 * Opens the printable / PDF product catalog in a new browser tab/window.
 * Targets /catalog/print — the same page launched by Open_Catalog_PDF.bat.
 * Works inside PyWebView (desktop app) and plain browser.
 * Falls back gracefully if the server is unreachable.
 */
function openCatalog() {
    const btn = document.getElementById('btn-open-catalog');
    const CATALOG_URL = 'http://localhost:5000/catalog/print';

    // Visual feedback — brief "launching" state
    if (btn) {
        btn.classList.add('launching');
        btn.textContent = '⏳ Opening…';
        setTimeout(() => {
            btn.classList.remove('launching');
            btn.textContent = '📄 Open Catalog';
        }, 1800);
    }

    // Probe the catalog/print endpoint first, then open
    fetch('/catalog/print', { method: 'HEAD' })
        .then(res => {
            if (res.ok || res.status === 302 || res.status === 200) {
                window.open(CATALOG_URL, '_blank', 'noopener,noreferrer');
            } else {
                _catalogError('Server returned status ' + res.status);
            }
        })
        .catch(() => {
            // Server unreachable — still try to open (user may have a separate browser)
            try {
                window.open(CATALOG_URL, '_blank', 'noopener,noreferrer');
            } catch (e) {
                _catalogError('Could not open catalog PDF tab.');
            }
        });
}

function _catalogError(msg) {
    // Lightweight toast — reuse existing showToast if present, else alert
    if (typeof showToast === 'function') {
        showToast('⚠️ Catalog: ' + msg, 'error');
    } else {
        alert('⚠️ Could not open the Product Catalog PDF.\n\nMake sure the billing app is running, then try again.\n\nDetail: ' + msg);
    }
    const btn = document.getElementById('btn-open-catalog');
    if (btn) {
        btn.classList.remove('launching');
        btn.textContent = '📄 Open Catalog';
    }
}

// Keyboard shortcut: Alt+C  →  Open Catalog PDF
document.addEventListener('keydown', function(e) {
    if (e.altKey && (e.key === 'c' || e.key === 'C')) {
        e.preventDefault();
        openCatalog();
    }
});
// ════════════════════════════════════════════════════

// ════════════════════════════════════════════════════
// TAB NAVIGATION
// ════════════════════════════════════════════════════
// ── Admin Sidebar Collapse Toggle ─────────────────────────────────────────────
function toggleAdminSidebar() {
    const sidebar = document.getElementById('admin-sidebar');
    if (sidebar) sidebar.classList.toggle('collapsed');
}

function setTab(tab) {
    ['billing','admin','analytics'].forEach(t => {
        document.getElementById(`section-${t}`).style.display = t===tab ? '' : 'none';
        document.getElementById(`tab-${t}`).classList.toggle('active', t===tab);
    });
    // Always clear the print-area when switching tabs so the receipt
    // never shows through behind the Admin / Analytics panels
    const pa = document.getElementById('print-area');
    if (pa) pa.innerHTML = '';
    // Scroll back to top when entering admin
    if (tab === 'admin') window.scrollTo(0, 0);
    if (tab==='analytics') loadAnalytics();
    if (tab==='admin')     { renderAdminList(); switchAdminTab(_lastAdminTab || 'products'); }
}

function switchAdminTab(tab) {
    _lastAdminTab = tab;
    ['products','stock','customers','loyalty','daily','packages','shortcuts','printer','credit'].forEach(t => {
        const panel = document.getElementById(`adm-${t}-panel`);
        if (panel) panel.style.display = t===tab ? '' : 'none';
        const btn = document.getElementById(`atab-${t}`);
        if (btn) btn.classList.toggle('active', t===tab);
    });
    if (tab === 'stock')     renderStockList();
    if (tab === 'loyalty')   loadLoyaltySettings();
    if (tab === 'daily')     setTodayAndLoad();
    if (tab === 'packages')  loadAdminPackages();
    if (tab === 'shortcuts') renderShortcutsAdmin();
    if (tab === 'printer')   renderPrinterAdmin();
    if (tab === 'customers') loadCustomers();
    if (tab === 'credit')    loadCreditReport();
}

// ════════════════════════════════════════════════════
// DAILY REPORT (Admin Tab)
// ════════════════════════════════════════════════════

// ── Admin: Shortcut Configuration Panel (v-patch) ─────────────────────────────
function renderShortcutsAdmin() {
    const panel = document.getElementById('adm-shortcuts-panel');
    if (!panel) return;
    const rows = Object.entries(_shortcuts).map(([action, key]) => {
        const label = _SHORTCUT_LABELS[action] || action;
        const displayKey = key
            .replace('ctrl+',  'Ctrl+')
            .replace('alt+',   'Alt+')
            .replace('shift+', 'Shift+')
            .replace(/^f(\d+)$/, 'F$1')
            .replace('escape',  'Esc');
        return `
        <div style="display:flex;align-items:center;gap:12px;padding:10px 0;
                    border-bottom:1px solid #f0eaff;">
          <span style="flex:1;font-size:13px;font-weight:600;">${label}</span>
          <input type="text" data-action="${action}" value="${displayKey}"
            style="width:140px;padding:7px 10px;border:1.5px solid #ddd;
                   border-radius:8px;font-size:13px;font-family:monospace;"
            placeholder="e.g. Ctrl+F" />
        </div>`;
    }).join('');

    panel.innerHTML = `
      <div class="admin-card">
        <h3>⌨️ Keyboard Shortcuts</h3>
        <p style="color:#888;font-size:12px;margin-bottom:12px;">
          Edit key combos below. Format: <code>ctrl+n</code>, <code>alt+p</code>,
          <code>f3</code>, <code>escape</code>. Changes are saved to localStorage.
        </p>
        <div id="shortcut-rows">${rows}</div>
        <div style="display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;">
          <button onclick="_saveShortcutsFromAdmin()"
            style="padding:10px 22px;background:#7C3AED;color:#fff;border:none;
                   border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;">
            💾 Save Shortcuts
          </button>
          <button onclick="_resetShortcutsToDefault()"
            style="padding:10px 22px;background:#f3f0ff;color:#7C3AED;
                   border:1.5px solid #7C3AED;border-radius:10px;
                   font-size:13px;font-weight:700;cursor:pointer;">
            ↩ Reset to Default
          </button>
        </div>
      </div>`;
}

function _saveShortcutsFromAdmin() {
    const inputs = document.querySelectorAll('#shortcut-rows input[data-action]');
    const newMap = {};
    inputs.forEach(inp => {
        // Normalise: "Ctrl+N" → "ctrl+n", "F3" → "f3", "Esc" → "escape"
        newMap[inp.dataset.action] = inp.value.trim().toLowerCase()
            .replace(/\s+/g, '').replace('meta+', 'ctrl+')
            .replace(/^esc$/, 'escape');
    });
    delete newMap.payUPI;
    delete newMap.payCash;
    _saveShortcuts(newMap);
    showToast('✅ Shortcuts saved!');
}

function _resetShortcutsToDefault() {
    if (!confirm('Reset all shortcuts to factory defaults?')) return;
    _saveShortcuts({ ..._DEFAULT_SHORTCUTS });
    renderShortcutsAdmin();
    showToast('↩ Shortcuts reset to defaults');
}
function setTodayAndLoad() {
    const d = new Date();
    const pad = n => String(n).padStart(2, '0');
    const isoDate = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
    const inp = document.getElementById('daily-date-input');
    if (inp) inp.value = isoDate;
    loadDailyReport();
}

async function loadDailyReport() {
    const inp = document.getElementById('daily-date-input');
    if (!inp || !inp.value) return;
    const [y, m, day] = inp.value.split('-');
    const dateStr = `${day}/${m}/${y}`;
    const el = document.getElementById('daily-report-result');
    if (!el) return;
    el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:20px 0;">⏳ Loading report…</div>';
    try {
        const r = await fetch(`${API}/summary/by-date?date=${encodeURIComponent(dateStr)}`);
        const d = await r.json();
        if (!d.success) throw new Error('API error');

        const billRows = d.bills.length
            ? d.bills.map(b => `
                <div style="display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid var(--border);font-size:13px;">
                  <div>
                    <span style="font-weight:600;">${b.created_at.split(' ')[1] || b.created_at}</span>
                    <span style="color:var(--muted);margin-left:8px;">${_h(b.customer_name || 'Walk-in')}${b.customer_phone ? ' · '+_h(b.customer_phone) : ''}</span>
                  </div>
                  <strong style="color:var(--blue);"><span class="currency">₹</span><span class="value">${Number(b.total).toFixed(2)}</span></strong>
                </div>`).join('')
            : '<p style="color:var(--muted);font-size:13px;padding:16px 0;">No bills recorded on this date.</p>';

        const topRows = d.top_products.length
            ? d.top_products.map((p, i) => `
                <div style="display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px;">
                  <span><span style="color:var(--muted);margin-right:8px;font-size:11px;">#${i+1}</span>${_h(p.name)}</span>
                  <span style="color:var(--green);font-weight:700;"><span class="currency">₹</span><span class="value">${Number(p.revenue).toFixed(2)}</span></span>
                </div>`).join('')
            : '<p style="color:var(--muted);font-size:13px;">No product data.</p>';

        const avg = d.total_bills > 0 ? (d.total_revenue / d.total_bills).toFixed(2) : '0.00';

        el.innerHTML = `
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:20px;">
            <div style="background:var(--green-lt);border:1.5px solid #A7F3D0;border-radius:12px;padding:16px;text-align:center;">
              <div style="font-size:10px;font-weight:800;color:var(--green);text-transform:uppercase;letter-spacing:.7px;margin-bottom:6px;">💰 Total Revenue</div>
              <div style="font-size:24px;font-weight:900;color:var(--green);"><span class="currency">₹</span><span class="value">${Number(d.total_revenue).toFixed(2)}</span></div>
            </div>
            <div style="background:var(--blue-xlt);border:1.5px solid var(--blue-lt);border-radius:12px;padding:16px;text-align:center;">
              <div style="font-size:10px;font-weight:800;color:var(--blue);text-transform:uppercase;letter-spacing:.7px;margin-bottom:6px;">🧾 Total Bills</div>
              <div style="font-size:24px;font-weight:900;color:var(--blue);">${d.total_bills}</div>
            </div>
            <div style="background:var(--amber-lt);border:1.5px solid #FDE68A;border-radius:12px;padding:16px;text-align:center;">
              <div style="font-size:10px;font-weight:800;color:var(--amber);text-transform:uppercase;letter-spacing:.7px;margin-bottom:6px;">📊 Avg Bill</div>
              <div style="font-size:24px;font-weight:900;color:var(--amber);"><span class="currency">₹</span><span class="value">${avg}</span></div>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
            <div>
              <h4 style="font-size:12px;font-weight:800;color:var(--text-2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px;">🏆 Top Products by Revenue</h4>
              ${topRows}
            </div>
            <div>
              <h4 style="font-size:12px;font-weight:800;color:var(--text-2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px;">🧾 All Bills (${d.total_bills})</h4>
              <div style="max-height:340px;overflow-y:auto;">${billRows}</div>
            </div>
          </div>`;
    } catch(e) {
        el.innerHTML = '<div style="color:var(--red);padding:12px 0;font-size:13px;">⚠️ Error loading report. Make sure the backend is running.</div>';
    }
}

// ════════════════════════════════════════════════════
// BILL LANGUAGE
// ════════════════════════════════════════════════════
function setBillLang(lang) {
    billLang = lang;
    document.getElementById('lang-en').classList.toggle('active', lang==='en');
    document.getElementById('lang-ta').classList.toggle('active', lang==='ta');
    // Show Tamil name field only in Tamil mode
    const taNamWrap = document.getElementById('customer-name-ta-wrap');
    if (taNamWrap) taNamWrap.style.display = lang === 'ta' ? 'flex' : 'none';
}

// ════════════════════════════════════════════════════
// LOAD PRODUCTS  (delegates to fetchProducts)
// ════════════════════════════════════════════════════
async function loadProducts() {
    // Show loading indicator before fetching
    const grid = document.getElementById('products-grid');
    if (grid) {
        grid.innerHTML = '<div class="state-msg" style="padding:40px; font-size:1.2rem; color:#166534;"><i class="fas fa-spinner fa-spin"></i> Loading Products...</div>';
    }
    // Defer the fetch by a tick so the browser can paint the initial UI
    return new Promise(resolve => {
        setTimeout(async () => {
            await fetchProducts();
            resolve();
        }, 50);
    });
}

// ════════════════════════════════════════════════════
// FETCH PRODUCTS (Legacy + Packages)
// ════════════════════════════════════════════════════
async function fetchProducts() {
    try {
        const [pr, pkgr] = await Promise.all([
            fetch(`${API}/products`),
            fetch(`${API}/packages`)
        ]);
        const d    = await pr.json();
        const pkgd = await pkgr.json();
        packages   = pkgd.packages || [];

        // Stable shortcode: use the product's DB id (e.g. "p52" → "P52")
        // This guarantees Quick Add works the same every session.
        // Also generate a 3-letter code as human-friendly alias but pin it to the DB id.
        const usedCodes = new Set();
        products = d.products.map(p => {
            p.usageCount = 0;
            // Primary stable code: DB id uppercased (e.g. p52 → P52)
            const stableCode = (p.id || '').toUpperCase();
            // Human-friendly 3-letter alias for the display label
            const latinPart = (p.name_english || p.name_en || p.name || '').replace(/[^a-zA-Z]/g,'');
            let chars = latinPart.substring(0,3).toUpperCase() || 'PRD';
            if (chars.length < 2) chars = 'PRD';
            let num = 1, friendlyCode = chars + num;
            while (usedCodes.has(friendlyCode)) { friendlyCode = chars.substring(0,2) + (++num); }
            usedCodes.add(friendlyCode);
            // Accept BOTH the stable id code and the friendly code in Quick Add
            p.shortcode      = stableCode;       // used for search/lookup
            p.shortcodeLabel = friendlyCode;     // shown on card
            p.displayName = buildDisplayName(p);
            return p;
        });
        // Apply active pricing mode prices to every product
        products = products.map(p => ({...p, price_per_kg: getEffectivePrice(p)}));
        buildCategoryPills();
        // Activate the current mode button
        setPricingMode(pricingMode);
        renderProducts();
        renderAdminList();
        // Clear any offline banner
        document.getElementById('offline-banner').style.display = 'none';
    } catch {
        // Silent retry once before showing offline banner
        try {
            const healthCheck = await fetch(`${API}/health`, {signal: AbortSignal.timeout(3000)});
            if (healthCheck.ok) {
                // Server is up, just a transient error — retry silently
                setTimeout(fetchProducts, 2000);
                return;
            }
        } catch {}
        showOfflineBanner();
    }
}

/** Formats display name as "English (Tamil)" — tanglish is NOT shown */
function buildDisplayName(p) {
    const en = (p.name_english || p.name_en || '').trim();
    const ta = (p.name_tamil  || p.name_ta  || '').trim();
    if (en && ta)  return `${en} (${ta})`;
    if (en)        return en;
    return p.name || '';
}

// ════════════════════════════════════════════════════
// SEARCH  –  English · Tamil · Tanglish · Synonyms
// ════════════════════════════════════════════════════

/** Build a single lowercase search corpus string for a product */
function _buildCorpus(p) {
    if (p._corpus) return p._corpus;  // cached
    const parts = [
        p.name_english || p.name_en || '',
        p.name_tamil   || p.name_ta || '',
        p.name_tanglish || '',
        p.displayName  || '',
        p.name         || '',
        p.search_tags  || '',
        p.shortcode    || '',
        p.category     || '',
    ];
    // Also include search_terms array if present
    if (Array.isArray(p.search_terms)) {
        p.search_terms.forEach(t => { if (typeof t === 'string') parts.push(t); });
    }
    p._corpus = parts.filter(Boolean).join(' ').toLowerCase();
    return p._corpus;
}

function matchesSearch(p, rawQ) {
    if (!rawQ || !rawQ.trim()) return true;
    const q = rawQ.toLowerCase().trim();
    const corpus = _buildCorpus(p);

    // 1. Direct substring — fastest, handles exact Tamil/English/Tanglish
    if (corpus.includes(q)) return true;

    // 2. Word-by-word: every word in query must appear somewhere in corpus
    //    "rose petal" → both "rose" AND "petal" must be in corpus
    const words = q.split(/\s+/).filter(w => w.length > 1);
    if (words.length > 1) {
        if (words.every(w => corpus.includes(w))) return true;
    }

    // 3. Synonym expansion: if query word has synonyms, check those too
    const syns = SEARCH_SYNONYMS[q] || [];
    for (const syn of syns) {
        if (corpus.includes(syn)) return true;
    }
    // Also check each query word individually against synonyms
    for (const w of words) {
        const wSyns = SEARCH_SYNONYMS[w] || [];
        for (const syn of wSyns) {
            if (corpus.includes(syn)) return true;
        }
    }

    // 4. Prefix match on each corpus word — "kuth" matches "kuthiraivali"
    if (q.length >= 3) {
        const corpusWords = corpus.split(/\s+/);
        for (const cw of corpusWords) {
            if (cw.startsWith(q)) return true;
        }
    }

    // 5. Fuzzy: query letters appear in sequence inside any corpus word (min 4 chars)
    if (q.length >= 4) {
        const corpusWords = corpus.split(/\s+/);
        for (const cw of corpusWords) {
            if (cw.length < q.length) continue;
            let ci = 0;
            let matched = true;
            for (const ch of q) {
                ci = cw.indexOf(ch, ci);
                if (ci < 0) { matched = false; break; }
                ci++;
            }
            if (matched) return true;
        }
    }

    return false;
}

// ════════════════════════════════════════════════════
// PRODUCT CARD HTML BUILDER  (shared by initial + lazy appends)
// ════════════════════════════════════════════════════
function _buildProductCard(p) {
    // Use product.image (DB field) directly — no name guessing.
    // getProductImagePath falls back to WEBP_NAME_MAP for products with no DB image.
    const localImg = getProductImagePath(p.name_english || p.name_en || p.name || '', p.image || '');
    const fallbackEmoji = p.emoji || '🌿';
    // On image error: hide img, show emoji span as fallback.
    const imgHtml = `<img src="${localImg}" alt="${p.displayName}" class="pcard-img" loading="lazy" width="120" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';this.onerror=null;"/><span class="pcard-emoji" style="display:none">${fallbackEmoji}</span>`;
    let stockBadge = '';
    const st = (p.stock !== null && p.stock !== undefined) ? parseFloat(p.stock) : null;
    if (st !== null) {
        if (st <= 0)  stockBadge = `<div class="pcard-stock pcard-stock-out">📭 Stock: 0 kg</div>`;
        else if (st < 5) stockBadge = `<div class="pcard-stock pcard-stock-low">⚠️ Low: ${st.toFixed(2)} kg</div>`;
        else             stockBadge = `<div class="pcard-stock pcard-stock-ok">📦 ${st.toFixed(2)} kg</div>`;
    }
    // No oosClass — card always fully clickable regardless of stock level
    // S.NO (catalog serial number) — shown as a small badge top-left of the card
    const sno = (p.serial_no !== null && p.serial_no !== undefined && p.serial_no !== '') ? p.serial_no : '';
    const snoBadge = sno !== '' ? `<span class="pcard-sno" title="Catalog S.NO">#${sno}</span>` : '';
    return `
        <div class="pcard" id="pcard-${p.id}" onclick="openWeightModal('${p.id}')" role="button">
          <div class="pcard-img-wrapper">
            ${imgHtml}
            ${snoBadge}
            <span class="pcard-badge">${p.category}</span>
          </div>
          <div class="pcard-info">
            <h3 class="pcard-title">${p.displayName}</h3>
            <div class="pcard-meta">
              <span class="pcard-price"><span class="currency">₹</span><span class="value">${p.price_per_kg}</span><small>/kg</small><span class="pcard-price-mode">${getPriceModeLabel()}</span></span>
              <span class="pcard-code">${p.shortcodeLabel || p.shortcode}</span>
            </div>
            ${stockBadge}
            <div class="pcard-action">
              <span class="tap-icon">➕</span><span class="tap-text">Tap to add</span>
            </div>
          </div>
        </div>`;
}

function _buildPackageCard(pkg) {
    const hasImg = pkg.image && pkg.image !== '';
    const imgHtml = hasImg
        ? `<img src="images/${pkg.image}" alt="${pkg.name_english||''}" class="pcard-img" loading="lazy" style="width:100%;height:100%;object-fit:cover;border-radius:inherit;" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';this.onerror=null;" /><span class="pcard-emoji" style="display:none">${pkg.emoji||'📦'}</span>`
        : `<span class="pcard-emoji">${pkg.emoji||'📦'}</span>`;
    return `
        <div class="pcard pcard-package" id="pcard-${pkg.id}" onclick="openPackageModal('${pkg.id}')" role="button">
          <div class="pcard-img-wrapper">
            ${imgHtml}
            <span class="pcard-badge" style="background:#7c3aed;">Bundle</span>
          </div>
          <div class="pcard-info">
            <h3 class="pcard-title">${pkg.name_english || pkg.name_en} (${pkg.name_tamil || pkg.name_ta})</h3>
            <div class="pcard-meta">
              <span class="pcard-price" style="color:#7c3aed;"><span class="currency">₹</span><span class="value">${pkg.price_per_kg}</span><small>/kg</small></span>
              <span class="pcard-code" style="background:#ede9fe;color:#7c3aed;">${pkg.items.length} items</span>
            </div>
            <div class="pcard-action">
              <span class="tap-icon">📦</span><span class="tap-text">Configure &amp; Add</span>
            </div>
          </div>
        </div>`;
}

// ════════════════════════════════════════════════════
// RENDER PRODUCTS  –  entry point (resets + lazy-loads first page)
// ════════════════════════════════════════════════════
function renderProducts(data) {
    if (data) {
        const arr = Array.isArray(data) ? data : (data.products || []);
        const usedCodes = new Set();
        products = arr.map(p => { p._corpus = null; // clear search cache
            p.usageCount = p.usageCount || 0;
            if (!p.shortcode) {
                const latinPart = (p.name_english || p.name_en || p.name || '').replace(/[^a-zA-Z]/g,'');
                let chars = latinPart.substring(0,3).toUpperCase() || 'PRD';
                if (chars.length < 2) chars = 'PRD';
                let num = 1, code = chars + num;
                while (usedCodes.has(code)) { code = chars.substring(0,2) + (++num); }
                usedCodes.add(code);
                p.shortcode      = (p.id || '').toUpperCase();  // stable DB-id code
                p.shortcodeLabel = code;                         // friendly display label
            }
            if (!p.displayName) p.displayName = buildDisplayName(p);
            return p;
        });
    }

    _kbIndex        = -1;
    _lazyRendered   = 0;
    _lazyLoading    = false;
    _lazyList       = [];

    // Tear down previous IntersectionObserver so it doesn't fire on stale sentinel
    if (_lazyObserver) { _lazyObserver.disconnect(); _lazyObserver = null; }

    const query = document.getElementById('productSearch').value;
    const grid  = document.getElementById('products-grid');

    // ── Build filtered + sorted list ──────────────────────────────────────────
    let list = [...products];
    // When searching, ignore category filter — search full product list
    const query_text = (query || '').trim().toLowerCase();
    if (query_text.length > 0) {
        // Search across ALL products regardless of category
        list = list.filter(p => matchesSearch(p, query));
    } else {
        // No search query: apply category filter
        if (activeCat !== 'All') {
            list = list.filter(p => (p.category || '').toLowerCase() === activeCat.toLowerCase());
        }
    }
    // PRIMARY ordering: catalog serial_no (S.NO). Fallback: name.
    // Products without a serial_no sort to the bottom in alphabetical order.
    list.sort((a, b) => {
        const sa = (a.serial_no === null || a.serial_no === undefined || a.serial_no === '') ? Infinity : Number(a.serial_no);
        const sb = (b.serial_no === null || b.serial_no === undefined || b.serial_no === '') ? Infinity : Number(b.serial_no);
        if (sa !== sb) return sa - sb;
        return (a.displayName || '').localeCompare(b.displayName || '');
    });
    _lazyList = list;

    // Update product count badge
    const _badge = document.getElementById('product-count-badge');
    if (_badge) _badge.textContent = list.length + ' product' + (list.length !== 1 ? 's' : '');

    // ── Package cards (always rendered fully — there are only 2) ─────────────
    let pkgHtml = '';
    if (activeCat === 'All' || activeCat === 'Packages') {
        const q = query.toLowerCase();
        const pkgsToShow = packages.filter(pkg => {
            if (!query) return true;
            const en = (pkg.name_english || pkg.name_en || '').toLowerCase();
            const ta = (pkg.name_tamil   || pkg.name_ta  || '');
            return en.includes(q) || ta.includes(q) || 'package'.includes(q);
        });
        pkgHtml = pkgsToShow.map(_buildPackageCard).join('');
    }

    // ── Nothing to show ───────────────────────────────────────────────────────
    if (!_lazyList.length && !pkgHtml) {
        grid.innerHTML = '<div class="state-msg">No products found 🌾</div>';
        return;
    }

    // ── Render first page immediately (fast initial paint) ───────────────────
    const firstBatch = _lazyList.slice(0, PAGE_SIZE);
    _lazyRendered    = firstBatch.length;

    grid.innerHTML   = pkgHtml + firstBatch.map(_buildProductCard).join('');

    // ── If there are more products, attach the IntersectionObserver ──────────
    if (_lazyRendered < _lazyList.length) {
        _attachLazySentinel(grid);
    } else {
        _removeLoadingIndicator();
    }

    _updateLazyCounter();
}

// ════════════════════════════════════════════════════
// LAZY  –  append next PAGE_SIZE products when sentinel enters view
// ════════════════════════════════════════════════════
function _attachLazySentinel(grid) {
    // Remove any old sentinel
    const old = document.getElementById('lazy-sentinel');
    if (old) old.remove();

    // Create a thin invisible div at the bottom of the grid
    const sentinel = document.createElement('div');
    sentinel.id    = 'lazy-sentinel';
    sentinel.style.cssText = 'height:1px;width:100%;grid-column:1/-1;';
    grid.appendChild(sentinel);

    // Observe it — fires when it scrolls into the viewport (+ 200px margin)
    _lazyObserver = new IntersectionObserver(entries => {
        if (!entries[0].isIntersecting || _lazyLoading) return;
        _appendNextPage();
    }, { root: grid, rootMargin: '0px 0px 200px 0px', threshold: 0 });

    _lazyObserver.observe(sentinel);
}

function _appendNextPage() {
    if (_lazyLoading) return;
    if (_lazyRendered >= _lazyList.length) {
        _removeLoadingIndicator();
        if (_lazyObserver) { _lazyObserver.disconnect(); _lazyObserver = null; }
        return;
    }
    _lazyLoading = true;

    const grid       = document.getElementById('products-grid');
    const sentinel   = document.getElementById('lazy-sentinel');
    const nextBatch  = _lazyList.slice(_lazyRendered, _lazyRendered + PAGE_SIZE);

    // Build fragment for efficient DOM insertion (one reflow)
    const frag = document.createDocumentFragment();
    const temp = document.createElement('div');
    temp.innerHTML = nextBatch.map(_buildProductCard).join('');
    while (temp.firstChild) frag.appendChild(temp.firstChild);

    // Insert before the sentinel so sentinel stays last
    if (sentinel) grid.insertBefore(frag, sentinel);
    else          grid.appendChild(frag);

    _lazyRendered += nextBatch.length;
    _lazyLoading   = false;

    _updateLazyCounter();

    // All done — detach observer and remove sentinel
    if (_lazyRendered >= _lazyList.length) {
        if (_lazyObserver) { _lazyObserver.disconnect(); _lazyObserver = null; }
        if (sentinel) sentinel.remove();
        _removeLoadingIndicator();
    }
}

function _updateLazyCounter() {
    let el = document.getElementById('lazy-counter');
    if (!el) {
        el = document.createElement('div');
        el.id = 'lazy-counter';
        el.style.cssText = [
            'grid-column:1/-1','text-align:center','font-size:11px',
            'color:#94a3b8','font-weight:600','padding:4px 0','letter-spacing:.3px'
        ].join(';');
        document.getElementById('products-grid').appendChild(el);
    }
    const total = _lazyList.length;
    const shown = Math.min(_lazyRendered, total);
    if (shown >= total) {
        el.textContent = total > 0 ? `Showing all ${total} products` : '';
    } else {
        el.textContent = `Showing ${shown} of ${total} products — scroll for more`;
    }
}

function _removeLoadingIndicator() {
    const el = document.getElementById('lazy-counter');
    if (el && _lazyRendered >= _lazyList.length) {
        const total = _lazyList.length;
        el.textContent = total > 0 ? `Showing all ${total} products` : '';
    }
}

function filterCat(cat, btn) {
    activeCat = cat;
    document.querySelectorAll('.cpill').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.cside').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    // Sync pill with matching data-cat
    const _pill = document.querySelector('.cpill[data-cat="' + cat + '"]');
    if (_pill) _pill.classList.add('active');
    const _sb = document.querySelector('.cside[data-cat="' + cat + '"]');
    if (_sb) _sb.classList.add('active');
    renderProducts();
}

// ════════════════════════════════════════════════════
// CUSTOMER PHONE LOOKUP
// ════════════════════════════════════════════════════
let _lookupTimer = null;
let _nameAutoFilled = false;   // track if name was auto-filled (so manual edits aren't wiped)
let _latestRepeatBill = null;



function _renderRepeatBillPreview(bill) {
    const panel = document.getElementById('repeat-bill-preview');
    if (!panel) return;
    if (!bill) {
        panel.style.display = 'none';
        panel.innerHTML = '';
        return;
    }

    const items = _getRepeatBillItems(bill.items).slice(0, 6);
    const rows = items.map(it => {
        const qty = it.is_package
            ? `${parseFloat(it.weight_kg != null ? it.weight_kg : ((it.weight_g || 0) / 1000) || 0).toFixed(2)} kg`
            : ((it.weight_g || 0) >= 1000
                ? `${((it.weight_g || 0) / 1000).toFixed(2)} kg`
                : `${it.weight_g || 0} g`);
        const name = it.name || it.name_english || it.name_tamil || 'Item';
        return `<div class="repeat-bill-item"><span class="repeat-bill-item-name">${name}</span><span class="repeat-bill-item-qty">${qty}</span></div>`;
    }).join('');

    const allItems = _getRepeatBillItems(bill.items);
    const extraNote = allItems.length > items.length
        ? `<div class="repeat-bill-note">+ ${allItems.length - items.length} more item${allItems.length - items.length > 1 ? 's' : ''}</div>`
        : `<div class="repeat-bill-note">Press Enter or click Repeat Bill to load this order</div>`;

    panel.innerHTML = `
      <div class="repeat-bill-head">
        <span>Latest Bill #${bill.id}</span>
        <span class="repeat-bill-date">${bill.created_at || bill.timestamp || ''}</span>
      </div>
      <div class="repeat-bill-items">${rows}</div>
      ${extraNote}`;
    panel.style.display = 'block';
}

async function _loadRepeatBillPreview(phone) {
    try {
        const r = await fetch(`${API}/admin/customers/${phone}/bills`);
        const d = await r.json();
        _latestRepeatBill = (d.bills || [])[0] || null;
        _renderRepeatBillPreview(_latestRepeatBill);
    } catch {
        _latestRepeatBill = null;
        _renderRepeatBillPreview(null);
    }
}

// POS behaviour: ENTER on phone field must NOT trigger any popup or auto-repeat.
// It silently loads customer details (already handled by lookupCustomer on input)
// and moves focus to the customer name field. Repeat is only via the
// "Repeat Bill" button or its assigned shortcut.
function handleRepeatBillPhoneKey(e) {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    e.stopPropagation();
    const phoneEl = e.target;
    const cleaned = (phoneEl?.value || '').replace(/\D/g, '');
    // Ensure preview is fetched silently (lookupCustomer already debounces this,
    // but trigger immediately on Enter so the side panel updates without waiting).
    if (cleaned.length >= 10) {
        try { _loadRepeatBillPreview(cleaned); } catch {}
    }
    const nameEl = document.getElementById('customer-name');
    if (nameEl) { nameEl.focus(); if (nameEl.select) nameEl.select(); }
}

function _getRepeatBillItems(rawItems) {
    if (Array.isArray(rawItems)) return rawItems;
    if (typeof rawItems === 'string') {
        try { return JSON.parse(rawItems); } catch { return []; }
    }
    return [];
}

function _rebuildCartFromPreviousBill(bill) {
    const billItems = _getRepeatBillItems(bill.items);
    const rebuilt = [];
    const missing = [];

    billItems.forEach(it => {
        if (it.is_package) {
            const pkg = packages.find(p => String(p.id) === String(it.product_id));
            if (!pkg) {
                missing.push(it.name || it.name_english || 'Package');
                return;
            }
            const activeItems = Array.isArray(it.active_items)
                ? it.active_items.map(ai => ({
                    ...ai,
                    qty_kg: parseFloat(ai.qty_kg != null ? ai.qty_kg : ((ai.weight_g || 0) / 1000)) || 0,
                    price_per_kg: parseFloat(ai.price_per_kg != null ? ai.price_per_kg : pkg.price_per_kg) || 0,
                })).filter(ai => ai.qty_kg > 0)
                : [];
            const qtyKg = parseFloat(it.weight_kg != null ? it.weight_kg : ((it.weight_g || 0) / 1000)) || 0;
            rebuilt.push({
                isPackage: true,
                pkg,
                qty_kg: qtyKg,
                activeItems,
                price: parseFloat(it.total_price) || 0,
                _nutritionDisplay: it.nutrition_display || null,
                _packageMode: it.package_mode || pkg.product_mode || 'normal',
            });
            return;
        }

        const product = products.find(p => String(p.id) === String(it.product_id));
        if (!product) {
            missing.push(it.name || it.name_english || 'Item');
            return;
        }
        const savedRate = parseFloat(it.price_per_kg);
        const liveRate  = parseFloat(product.price_per_kg);
        const tempRate  = (!isNaN(savedRate) && !isNaN(liveRate) && savedRate !== liveRate) ? savedRate : null;
        rebuilt.push({
            product,
            weight_g: parseFloat(it.weight_g) || 0,
            price: parseFloat(it.total_price) || calcPrice(tempRate ?? liveRate, parseFloat(it.weight_g) || 0),
            temp_price_per_kg: tempRate,
        });
    });

    return { rebuilt, missing };
}

async function repeatLatestBillFromPhone() {
    const phoneEl = document.getElementById('customer-phone');
    const nameEl  = document.getElementById('customer-name');
    const cleaned = (phoneEl?.value || '').replace(/\D/g, '');

    if (cleaned.length < 10) {
        showToast('Enter valid customer phone number');
        return;
    }

    try {
        let latestBill = _latestRepeatBill;
        if (!latestBill || String(latestBill.customer_phone || '').replace(/\D/g, '') !== cleaned) {
            const r = await fetch(`${API}/admin/customers/${cleaned}/bills`);
            const d = await r.json();
            latestBill = (d.bills || [])[0] || null;
            _latestRepeatBill = latestBill;
            _renderRepeatBillPreview(latestBill);
        }
        if (!latestBill) {
            showToast('No previous bill found');
            return;
        }

        if (cart.length && !confirm('Replace current cart with previous bill?')) return;

        const { rebuilt, missing } = _rebuildCartFromPreviousBill(latestBill);
        if (!rebuilt.length) {
            showToast('Previous bill items could not be restored');
            return;
        }

        // ── Full state reset before loading repeat data ───────────────────
        cart = [];
        currentBill = null;
        applyLoyaltyReward = false;
        _nameAutoFilled = false;
        // Reset payment method to cash
        _currentPaymentMethod = 'cash';
        ['cash','upi','credit'].forEach(m => {
            const btn = document.getElementById('pm-' + m);
            if (btn) btn.classList.toggle('active', m === 'cash');
        });
        const paidInp = document.getElementById('paid-amount-input');
        const cashInp = document.getElementById('cash-input');
        if (paidInp) paidInp.value = '';
        if (cashInp) cashInp.value = '';

        cart = rebuilt;
        currentBill = null;
        if (nameEl && latestBill.customer_name) {
            nameEl.value = latestBill.customer_name;
            _nameAutoFilled = true;
        }
        renderCart();
        updateTotals();
        // Ensure user is on billing tab (they may have triggered R shortcut from admin)
        if (typeof setTab === 'function') setTab('billing');
        focusSearch();

        // pywebview repaint nudge
        requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));

        if (missing.length) {
            showToast(`Repeat bill loaded with ${missing.length} missing item${missing.length > 1 ? 's' : ''}`);
        } else {
            showToast(`Repeat bill loaded: ${rebuilt.length} item${rebuilt.length > 1 ? 's' : ''}`);
        }
    } catch {
        showToast('Could not load previous bill');
    }
}

// ════════════════════════════════════════════════════
// LOYALTY STARS PANEL  (shown below customer bar)
// ════════════════════════════════════════════════════
function _showLoyaltyPanel(d) {
    const panel = document.getElementById('loyalty-panel');
    if (!panel) return;
    if (!d || !d.loyalty_enabled) { panel.style.display = 'none'; return; }

    // Safe defaults
    const starsFor    = d.stars_for_reward || _loyaltySettings.stars_required || 5;
    const discount    = d.reward_discount  || _loyaltySettings.discount        || 100;
    const minPurchase = d.min_purchase     || _loyaltySettings.min_purchase    || 500;
    const stars       = Math.min(d.stars   || 0, starsFor);
    const remaining   = starsFor - stars;
    const rewardReady = remaining === 0;

    // Check current cart total to decide if Apply is allowed
    const cartTotal   = cart.reduce((s, i) => s + i.price, 0);
    const canApply    = rewardReady && cartTotal >= minPurchase;

    applyLoyaltyReward = false;

    const hintText = rewardReady
        ? `\uD83C\uDF89 Reward Ready! \u20B9${discount} discount available`
        : `Earn ${remaining} more star${remaining > 1 ? 's' : ''} to get \u20B9${discount} discount`;

    let applyBtn = '';
    if (rewardReady) {
        if (canApply) {
            applyBtn = `<button id="loyalty-apply-btn" onclick="toggleLoyaltyReward(true)"
                style="margin-top:6px;width:100%;padding:6px;background:#28a745;color:#fff;border:none;border-radius:6px;font-weight:700;cursor:pointer;font-size:13px;">
                Apply \u20B9${discount} Discount
               </button>`;
        } else {
            applyBtn = `<div style="margin-top:6px;width:100%;padding:5px 8px;background:#fff3cd;color:#856404;border-radius:6px;font-size:11px;text-align:center;">
                \u26A0\uFE0F Add \u20B9${minPurchase} or more to use reward
               </div>`;
        }
    }

    panel.innerHTML = `
      <div class="row">
        <span>\u2B50 Loyalty</span>
        <span></span>
        <span>${stars} / ${starsFor}</span>
      </div>
      <div class="row small-text">
        <span>${hintText}</span>
        <span></span>
        <span></span>
      </div>
      ${applyBtn}`;
    panel.style.display = 'block';
}

function _hideLoyaltyPanel() {
    // Loyalty panel is always visible — only reset the reward flag
    applyLoyaltyReward = false;
}

// Refreshes just the Apply button area based on current cart total.
// Called from renderCart() so button appears/disappears as items are added/removed.
function _refreshLoyaltyApplyBtn() {
    const panel = document.getElementById('loyalty-panel');
    if (!panel || panel.style.display === 'none') return;
    const existingBtn = document.getElementById('loyalty-apply-btn');
    const existingWarn = panel.querySelector('[data-min-warn]');
    // Find the reward-ready state from existing panel content
    const starsSpan = panel.querySelector('.row span:last-child');
    if (!starsSpan) return;
    const parts = (starsSpan.textContent || '').split('/');
    if (parts.length < 2) return;
    const stars   = parseInt(parts[0].trim()) || 0;
    const starsFor = parseInt(parts[1].trim()) || _loyaltySettings.stars_required || 5;
    if (stars < starsFor) return; // reward not ready, nothing to refresh

    const minPurchase = _loyaltySettings.min_purchase || 500;
    const discount    = _loyaltySettings.discount || 100;
    const cartTotal   = cart.reduce((s, i) => s + i.price, 0);
    const canApply    = cartTotal >= minPurchase;

    // Remove existing btn/warning
    if (existingBtn) existingBtn.remove();
    if (existingWarn) existingWarn.remove();

    if (canApply) {
        const btn = document.createElement('button');
        btn.id = 'loyalty-apply-btn';
        // Preserve already-applied state — don't reset it just because cart changed
        if (applyLoyaltyReward) {
            btn.textContent = '\u2713 Discount Applied \u2014 click to undo';
            btn.style.cssText = 'margin-top:6px;width:100%;padding:6px;background:#6c757d;color:#fff;border:none;border-radius:6px;font-weight:700;cursor:pointer;font-size:13px;';
            btn.onclick = () => toggleLoyaltyReward(false);
        } else {
            btn.textContent = `Apply \u20B9${discount} Discount`;
            btn.style.cssText = 'margin-top:6px;width:100%;padding:6px;background:#28a745;color:#fff;border:none;border-radius:6px;font-weight:700;cursor:pointer;font-size:13px;';
            btn.onclick = () => toggleLoyaltyReward(true);
        }
        panel.appendChild(btn);
    } else {
        applyLoyaltyReward = false; // can't apply if below minimum
        const warn = document.createElement('div');
        warn.setAttribute('data-min-warn', '1');
        warn.style.cssText = 'margin-top:6px;width:100%;padding:5px 8px;background:#fff3cd;color:#856404;border-radius:6px;font-size:11px;text-align:center;';
        warn.textContent = `\u26A0\uFE0F Add \u20B9${minPurchase} or more to use reward`;
        panel.appendChild(warn);
    }
}

// Step 7 — Update star count in loyalty panel after save
// Now delegates to _updateLoyaltyAfterSave which uses currentBill directly.
// stars / starsFor args kept for backwards compat with any external callers.
function updateStarDisplay(stars, starsFor) {
    if (window.currentBill) {
        _updateLoyaltyAfterSave(window.currentBill);
        return;
    }
    // Fallback: minimal panel update when no currentBill yet
    const panel = document.getElementById('loyalty-panel');
    if (!panel || panel.style.display === 'none') return;
    starsFor = starsFor || _loyaltySettings.stars_required || 5;
    const rewardAmt = _loyaltySettings.discount || 100;
    const remaining = starsFor - stars;
    const hintText  = remaining > 0
        ? `Earn ${remaining} more star${remaining > 1 ? 's' : ''} to get \u20B9${rewardAmt} discount`
        : `\uD83C\uDF89 Reward Ready! \u20B9${rewardAmt} discount available`;
    panel.innerHTML = `
      <div class="row">
        <span>\u2B50 Loyalty</span>
        <span></span>
        <span>${stars} / ${starsFor}</span>
      </div>
      <div class="row small-text">
        <span>${hintText}</span>
        <span></span>
        <span></span>
      </div>`;
}

function toggleLoyaltyReward(apply) {
    applyLoyaltyReward = apply;
    const btn = document.getElementById('loyalty-apply-btn');
    const rewardAmt = _loyaltySettings.discount || 100;
    if (apply) {
        if (btn) {
            btn.textContent = '✓ Discount Applied — click to undo';
            btn.style.background = '#6c757d';
            btn.onclick = () => toggleLoyaltyReward(false);
        }
        showToast(`\uD83C\uDF81 \u20B9${rewardAmt} discount will be applied on Save!`);
    } else {
        if (btn) {
            btn.textContent = `Apply \u20B9${rewardAmt} Discount`;
            btn.style.background = '#28a745';
            btn.onclick = () => toggleLoyaltyReward(true);
        }
        showToast('Discount removed.');
    }
    updateTotals();  // recalc after loyalty change
}

// ════════════════════════════════════════════════════
// WEIGHT MODAL
// ════════════════════════════════════════════════════
function openWeightModal(productId) {
    modalProduct = products.find(p => p.id === productId);
    if (!modalProduct) return;

    // Stock is informational only — low/zero stock shows a badge on the card
    // but does NOT block adding the product to the bill
    document.getElementById('wt-emoji').textContent = modalProduct.emoji || '🌿';
    document.getElementById('wt-name').textContent  = modalProduct.displayName;
    document.getElementById('wt-price').innerHTML = `<span class="currency">₹</span><span class="value">${modalProduct.price_per_kg}/kg (${getPriceModeLabel()})</span>`;
    document.getElementById('wt-input').value       = '1';
    document.getElementById('wt-unit').value        = 'kg';
    document.getElementById('wt-temp-price').value  = '';
    document.querySelectorAll('.qbtn').forEach(b => b.classList.remove('active'));
    updatePreview();
    document.getElementById('wt-overlay').classList.add('open');
    document.getElementById('wt-modal').classList.add('open');
    // TASK 1: Auto-focus and select the quantity input after modal renders
    setTimeout(() => {
        const qty = document.getElementById('wt-input');
        if (qty) { qty.focus(); qty.select(); }
    }, 50);
}

function closeWeightModal() {
    document.getElementById('wt-overlay').classList.remove('open');
    document.getElementById('wt-modal').classList.remove('open');
    modalProduct = null;
}

// ════════════════════════════════════════════════════
// PACKAGE MODAL  — per-item individual kg inputs
// ════════════════════════════════════════════════════
function openPackageModal(pkgId) {
    const pkg = packages.find(p => p.id === pkgId);
    if (!pkg) return;
    // Calculate base weight (sum of all ratio_kg)
    const baseWeight = pkg.items.reduce((s, it) => s + (it.ratio_kg || 0), 0);
    pkgModal = {
        pkg,
        baseWeight,
        userQty:     1,   // default 1 kg
        activeItems: pkg.items.map((_, i) => i)   // all checked by default
    };
    renderPackageModal();
    document.getElementById('pkg-overlay').classList.add('open');
    document.getElementById('pkg-modal').classList.add('open');
    // Focus qty input
    setTimeout(() => {
        const inp = document.getElementById('pkg-user-qty');
        if (inp) { inp.focus(); inp.select(); }
    }, 60);
}

function closePackageModal() {
    document.getElementById('pkg-overlay').classList.remove('open');
    document.getElementById('pkg-modal').classList.remove('open');
    pkgModal = null;
}


function setPkgQty(qty) {
    if (!pkgModal) return;
    pkgModal.userQty = qty;
    document.getElementById('pkg-user-qty').value = qty;
    renderPackageModal();
}

function onPkgUserQty(val) {
    if (!pkgModal) return;
    const v = parseFloat(val);
    pkgModal.userQty = (!isNaN(v) && v > 0) ? v : 0;
    renderPackageModal();
}

function renderPackageModal() {
    if (!pkgModal) return;
    const {pkg, activeItems, userQty, baseWeight} = pkgModal;
    document.getElementById('pkg-modal-title').textContent =
        `${pkg.name_english || pkg.name_en} (${pkg.name_tamil || pkg.name_ta})`;

    // Calculate scaling factor
    const factor = baseWeight > 0 ? userQty / baseWeight : 0;
    
    // Selling price = pkg.price_per_kg (the admin-saved rate, auto or manual override)
    // This is always the correct price to show to the customer.
    const sellingPricePerKg = pkg.price_per_kg || 0;
    const totalPrice = userQty * sellingPricePerKg;

    // Also compute ingredient cost for reference display
    const ingredientCost = activeItems.reduce((s, idx) => {
        const ppk = pkg.items[idx].price_per_kg || 0;
        const scaledKg = (pkg.items[idx].ratio_kg || 0) * factor;
        return s + scaledKg * ppk;
    }, 0);

    if (userQty > 0) {
        document.getElementById('pkg-preview').innerHTML =
            `<strong>${userQty} kg</strong> = ` +
            `<strong style="color:var(--green)"><span class="currency">₹</span><span class="value">${totalPrice.toFixed(2)}</span></strong>` +
            (Math.abs(ingredientCost - totalPrice) > 0.5
                ? `<span style="color:var(--muted);font-size:11px;margin-left:8px;">(cost ₹${ingredientCost.toFixed(2)})</span>`
                : '');
    } else {
        document.getElementById('pkg-preview').innerHTML =
            `<span style="color:#999;">Enter quantity above</span>`;
    }

    // Items list — show ingredient names with checkbox (read-only, auto-scaled)
    document.getElementById('pkg-items-list').innerHTML = pkg.items.map((item, idx) => {
        const checked    = activeItems.includes(idx);
        const nameLabel  = item.name_english
            ? `${item.name_tamil || item.name_ta} <span style="color:#888;font-size:11px;">(${item.name_english})</span>`
            : (item.name_tamil || item.name_ta);
        const isOil = _isOilIngredientJS(item.name_english || '');
        const oilBadge = isOil
            ? `<span style="font-size:9px;background:#fef3c7;color:#d97706;border-radius:3px;padding:1px 4px;margin-left:3px;">oil</span>`
            : '';
        return `
        <div class="pkg-item-row${checked ? '' : ' pkg-item-removed'}"
             style="display:flex;align-items:center;gap:8px;padding:6px 4px;border-bottom:1px solid #f0f0f0;">
          <input type="checkbox" ${checked ? 'checked' : ''} onchange="togglePkgItem(${idx}, this.checked)"
                 style="width:16px;height:16px;accent-color:#7c3aed;flex-shrink:0;"/>
          <span style="font-size:14px;">•</span>
          <span class="pkg-item-name" style="flex:1;font-size:13px;font-weight:500;">
            ${nameLabel}${oilBadge}
          </span>
        </div>`;
    }).join('');

    // Trigger live nutrition update whenever modal re-renders
    _updatePkgNutritionPreview();
}

// ── JS-side oil seed detection (mirrors backend _is_oil_ingredient) ───────────
const _OIL_KW_JS = [
    'groundnut','peanut','sunflower','sesame','flax','niger','mustard','chia',
    'rapeseed','canola','safflower','sappola','sapola','walnut','cashew',
    'almond','pista','pistachio','soybean','soya','poppy','pumpkin seed'
];
function _isOilIngredientJS(name) {
    const lc = (name || '').toLowerCase();
    return _OIL_KW_JS.some(kw => lc.includes(kw));
}

// ── Live nutrition preview — debounced API call to /api/package-nutrition ─────
let _pkgNutrTimer = null;
function _updatePkgNutritionPreview() {
    if (!pkgModal) return;
    clearTimeout(_pkgNutrTimer);
    _pkgNutrTimer = setTimeout(async () => {
        const {pkg, activeItems, userQty, baseWeight} = pkgModal;
        const factor = baseWeight > 0 ? userQty / baseWeight : 0;
        const items = activeItems
            .map(idx => ({
                name_english:      pkg.items[idx].name_english || pkg.items[idx].name_en || '',
                name_tamil:        pkg.items[idx].name_tamil   || pkg.items[idx].name_ta  || '',
                weight_kg:         (pkg.items[idx].ratio_kg || 0) * factor,
                nutrients_per_100g: pkg.items[idx].nutrients_per_100g || {},
            }))
            .filter(it => it.weight_kg > 0);

        const panel = document.getElementById('pkg-nutr-panel');
        const rows  = document.getElementById('pkg-nutr-rows');
        if (!panel || !rows) return;

        if (items.length === 0) { panel.style.display = 'none'; return; }

        try {
            const resp = await fetch(`${API}/api/package-nutrition`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ items })
            });
            const d = await resp.json();
            if (!d.success) { panel.style.display = 'none'; return; }

            const n       = d.nutrition || {};
            const hasOil  = !!d.has_oil;
            const MIN_VAL = 0.5;

            // Build nutrient rows
            const pkgProductMode = document.getElementById('pkg-product-mode') ? document.getElementById('pkg-product-mode').value : 'normal';
            const nutKeys = [
                ['Protein',       'protein'],
                ['Fat',           'fat'],
                ['Carbohydrates', 'carbohydrates'],
                ['Fiber',         'fiber'],
                ['Moisture',      'moisture'],
                ['Ash',           'ash'],
            ];

            let html = '';
            for (const [label, key] of nutKeys) {
                const v = parseFloat(n[key] || 0);
                // moisture: show only for oil packages
                if (key === 'moisture') {
                    const _useOilMode = pkgProductMode === 'oil' || hasOil;
                    if (!_useOilMode) continue;   // hide entirely for non-oil packages
                    if (v <= 0)  continue;   // still skip true zero
                } else if (key === 'ash') {
                    // ash: always show when non-zero
                    if (v <= 0) continue;
                } else {
                    if (v < MIN_VAL) continue;  // standard threshold for other nutrients
                }
                const bar = Math.min(Math.round(v), 100);
                html +=
                    `<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px;">` +
                    `<span style="min-width:100px;color:#166534;">${label}</span>` +
                    `<div style="flex:1;height:6px;background:#dcfce7;border-radius:3px;overflow:hidden;">` +
                    `<div style="width:${bar}%;height:100%;background:#16a34a;border-radius:3px;"></div></div>` +
                    `<span style="min-width:44px;text-align:right;font-weight:700;color:#15803d;">` +
                    `${v.toFixed(1)} %</span>` +
                    `</div>`;
            }
            if (!html) html = '<span style="color:#888;">No nutrition data available</span>';

            const oilNote = hasOil
                ? `<div style="font-size:10px;color:#d97706;margin-top:4px;">⚠️ Contains oil seeds — moisture &amp; ash included</div>`
                : '';

            rows.innerHTML = html + oilNote;
            panel.style.display = 'block';

        } catch (err) {
            panel.style.display = 'none';
        }
    }, 350);  // 350ms debounce — avoids hammering API on every keystroke
}

function togglePkgItem(idx, checked) {
    if (!pkgModal) return;
    if (checked) {
        if (!pkgModal.activeItems.includes(idx)) pkgModal.activeItems.push(idx);
    } else {
        pkgModal.activeItems = pkgModal.activeItems.filter(i => i !== idx);
    }
    renderPackageModal();
}

// onPkgItemQty removed — scaling is automatic now

// Enter key in pkg qty input → confirm
function pkgQtyKeydown(e) {
    if (e.key === 'Escape') { e.preventDefault(); closePackageModal(); return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    confirmAddPackage();
}

function confirmAddPackage() {
    if (!pkgModal) return;

    const {pkg, activeItems, userQty, baseWeight} = pkgModal;
    if (activeItems.length === 0) { showToast('⚠️ Select at least one item'); return; }
    if (!userQty || userQty <= 0) { showToast('⚠️ Enter a valid quantity'); return; }

    // Calculate scaling factor from base package weight
    const factor = baseWeight > 0 ? userQty / baseWeight : 0;

    // Scale each active ingredient
    const activeItemsData = activeItems
        .map(idx => ({ ...pkg.items[idx], qty_kg: parseFloat(((pkg.items[idx].ratio_kg || 0) * factor).toFixed(4)) }))
        .filter(it => it.qty_kg > 0);

    if (activeItemsData.length === 0) { showToast('⚠️ No valid ingredients'); return; }

    const totalKg    = userQty;
    // Use the package's saved selling price (admin-set, auto-calc or manual override)
    const totalPrice = calcPackagePrice(activeItemsData.reduce((s, i) => s + (Number(i.qty_kg) || 0), 0), pkg.price_per_kg || 0);

    // Build the cart entry — nutrition will be fetched asynchronously and cached
    const cartEntry = {
        isPackage:          true,
        pkg,
        qty_kg:             totalKg,
        activeItems:        activeItemsData,   // each item now has .qty_kg
        price:              totalPrice,
        _nutritionDisplay:  null,              // filled below after API call
        _hasOil:            false,
        _packageMode:       pkg.product_mode || 'normal',
    };
    cart.push(cartEntry);
    window.currentBill = null;  // new package — invalidate server total
    renderCart();
    showToast(`✓ ${pkg.name_english || pkg.name_en} (${totalKg.toFixed(2)} kg) added`);
    closePackageModal();

    // Async: fetch package nutrition and cache on the cart item for receipt use
    const nutrItems = activeItemsData.map(it => ({
        name_english:       it.name_english || it.name_en || '',
        name_tamil:         it.name_tamil   || it.name_ta  || '',
        weight_kg:          it.qty_kg,
        nutrients_per_100g: it.nutrients_per_100g || {},
    }));
    fetch(`${API}/api/package-nutrition`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ items: nutrItems })
    })
    .then(r => r.json())
    .then(d => {
        if (d.success) {
            cartEntry._nutritionDisplay = d.nutrition;
            cartEntry._hasOil          = !!d.has_oil;
            cartEntry._hasNormal       = !!d.has_normal;
        }
    })
    .catch(() => { /* silent — receipt will use server nutrition after save */ });
}

function removePackageFromCart(idx) {
    cart.splice(idx, 1);
    window.currentBill = null;  // cart changed — invalidate server total
    renderCart();
}

function adjustPkgCartQty(idx, itemName, delta) {
    const cartItem = cart[idx];
    if (!cartItem || !cartItem.isPackage) return;
    const it = cartItem.activeItems.find(i => (i.name_tamil || i.name_ta) === itemName);
    if (!it) return;
    it.qty_kg = Math.max(0, parseFloat((it.qty_kg + delta).toFixed(2)));
    cartItem.qty_kg = cartItem.activeItems.reduce((s, i) => s + i.qty_kg, 0);
    // Recalculate total using the package's selling price_per_kg
    cartItem.price = calcPackagePrice(cartItem.qty_kg, cartItem.pkg.price_per_kg || 0);
    window.currentBill = null;  // cart changed — invalidate server total
    renderCart();
}

function setQuick(grams, btn) {
    document.getElementById('wt-input').value = grams >= 1000 ? grams/1000 : grams;
    document.getElementById('wt-unit').value  = grams >= 1000 ? 'kg' : 'g';
    document.querySelectorAll('.qbtn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    updatePreview();
}

function handleUnitChange() {
    const unit = document.getElementById('wt-unit').value;
    const val  = parseFloat(document.getElementById('wt-input').value) || 0;
    if (unit==='kg' && val>100)        document.getElementById('wt-input').value = +(val/1000).toFixed(3);
    else if (unit==='g' && val<1&&val>0) document.getElementById('wt-input').value = Math.round(val*1000);
    updatePreview();
}

function getWeightInGrams() {
    const val  = parseFloat(document.getElementById('wt-input').value) || 0;
    const unit = document.getElementById('wt-unit').value;
    return unit==='kg' ? val*1000 : val;
}

function updatePreview() {
    if (!modalProduct) return;
    const wt_g       = getWeightInGrams();
    const wt_kg      = wt_g / 1000;
    const tempVal    = parseFloat(document.getElementById('wt-temp-price').value);
    const pPerKg     = isNaN(tempVal) ? modalProduct.price_per_kg : tempVal;
    const price      = (wt_kg * pPerKg).toFixed(2);
    const wtLabel    = wt_g >= 1000 ? `${wt_kg.toFixed(3)} kg` : `${wt_g} g`;
    document.getElementById('wt-preview').innerHTML =
        `${wtLabel} × <span class="currency">₹</span><span class="value">${pPerKg}/kg</span> = <strong style="color:var(--green)"><span class="currency">₹</span><span class="value">${price}</span></strong>` +
        (!isNaN(tempVal) ? ` <em style="color:orange;font-size:11px;">(temp price)</em>` : '');
}

function confirmAdd() {
    if (!modalProduct) return;
    const wt_g = getWeightInGrams();
    if (wt_g <= 0) { showToast('⚠️ Enter valid weight'); return; }
    const tv   = parseFloat(document.getElementById('wt-temp-price').value);
    addToCart(modalProduct, wt_g, isNaN(tv) ? null : tv);
    closeWeightModal();
}

// ════════════════════════════════════════════════════
// CART OPERATIONS
// ════════════════════════════════════════════════════
// ── _showDuplicateToast (v-patch) ─────────────────────────────────────────────
// Lightweight non-blocking banner; auto-dismisses in 1.5 s then fires onConfirm.
function _showDuplicateToast(itemName, onConfirm) {
    // v-patch: blocking YES/NO confirmation modal
    document.getElementById('dup-toast')?.remove();
    const overlay = document.createElement('div');
    overlay.id = 'dup-toast';
    overlay.style.cssText = [
        'position:fixed', 'inset:0', 'background:rgba(0,0,0,0.55)',
        'z-index:10000', 'display:flex', 'align-items:center', 'justify-content:center'
    ].join(';');
    const dismiss  = () => overlay.remove();
    const confirm_ = () => { overlay.remove(); onConfirm(); };
    overlay.innerHTML = `
        <div style="background:#1e1b4b;color:#fff;padding:24px 28px;border-radius:16px;
                    max-width:340px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,0.5);
                    border:1.5px solid #7C3AED;text-align:center;font-family:inherit;">
            <div style="font-size:26px;margin-bottom:10px;">🛒</div>
            <p style="margin:0 0 6px;font-size:15px;font-weight:700;">Item already in cart</p>
            <p style="margin:0 0 20px;font-size:13px;color:#c4b5fd;">
                <strong>${itemName}</strong><br>Do you want to add it again?
            </p>
            <div style="display:flex;gap:12px;justify-content:center;">
                <button id="dup-no"  style="flex:1;padding:10px;border-radius:8px;border:1.5px solid #7C3AED;
                       background:transparent;color:#c4b5fd;font-weight:700;cursor:pointer;font-size:14px;">✗ NO</button>
                <button id="dup-yes" style="flex:1;padding:10px;border-radius:8px;border:none;
                       background:#7C3AED;color:#fff;font-weight:700;cursor:pointer;font-size:14px;">✓ YES, ADD</button>
            </div>
        </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('#dup-yes').addEventListener('click', confirm_);
    overlay.querySelector('#dup-no').addEventListener('click', dismiss);
    overlay.addEventListener('click', e => { if (e.target === overlay) dismiss(); });
    const _kh = e => {
        if (e.key === 'Enter')  { document.removeEventListener('keydown', _kh); confirm_(); }
        if (e.key === 'Escape') { document.removeEventListener('keydown', _kh); dismiss();  }
    };
    document.addEventListener('keydown', _kh);
}

function addToCart(product, weight_g, temp_price=null) {
    // Stock is informational only — no block on add, stock badge on card is the indicator
    const _st = (product.stock !== null && product.stock !== undefined) ? Number(product.stock) : null;
    product.usageCount = (product.usageCount||0)+1;
    renderProducts();
    const pricePerKg = temp_price !== null ? temp_price : getEffectivePrice(product);
    // Guard: skip package items in cart (they have no .product property)
    const existing   = cart.find(i => i.product && i.product.id===product.id);
    if (existing && temp_price===null) {
        // v-patch: show duplicate popup, then increment after dismiss
        _showDuplicateToast(product.displayName || product.name_english, () => {
            existing.weight_g = parseFloat((existing.weight_g + weight_g).toFixed(1));
            existing.price    = calcPrice(_getCartItemPricePerKg(existing), existing.weight_g);
            window.currentBill = null;  // qty changed — invalidate server total
            renderCart();
        });
    } else {
        cart.push({ product, weight_g:parseFloat(weight_g.toFixed(1)),
                    price:calcPrice(pricePerKg,weight_g), temp_price_per_kg:temp_price });
        window.currentBill = null;  // new item — invalidate server total
        renderCart();
        showToast(`✓ ${product.displayName} added`);
    }
    // Auto-focus search for keyboard-only workflow
    setTimeout(() => {
        const s = document.getElementById('productSearch');
        if (s) { s.focus(); s.select(); }
    }, 0);
}

function removeFromCart(pid) {
    cart = cart.filter(i => !i.product || i.product.id!==pid);
    window.currentBill = null;  // cart changed — invalidate server total
    renderCart();
}

function adjustQty(pid, deltaG) {
    const item = cart.find(i => i.product && i.product.id===pid);
    if (!item) return;
    const newWt = parseFloat((item.weight_g+deltaG).toFixed(1));
    if (newWt <= 0) { removeFromCart(pid); return; }
    // Stock is informational only — no quantity cap
    item.weight_g = newWt;
    item.price = calcPrice(_getCartItemPricePerKg(item), item.weight_g);
    window.currentBill = null;  // qty changed — invalidate server total
    renderCart();
}

function handleDirectQtyInput(input) {
    const pid = input.dataset.pid;
    const unit = input.dataset.unit;
    let val = parseFloat(input.value);
    if (isNaN(val) || val < 0) val = 0;
    const newWeightG = unit === 'kg' ? val * 1000 : val;
    if (newWeightG <= 0) { removeFromCart(pid); return; }
    const item = cart.find(i => i.product && i.product.id === pid);
    if (!item) return;
    // Stock is informational only — no cap on direct qty entry
    item.weight_g = parseFloat(newWeightG.toFixed(1));
    item.price = calcPrice(_getCartItemPricePerKg(item), item.weight_g);
    window.currentBill = null;  // qty changed — invalidate server total
    renderCart();
}

// AUDIT FIX F7 — exact money arithmetic, identical to backend app.py::line_amount():
// price_per_kg × weight_g / 1000 computed in exact decimal, rounded half-up to paise.
// (The old float toFixed() disagreed with the saved bill by 1 paisa in ~1.2% of cases.)
function _decParts(x) {
    // Shortest round-trip decimal string (same digits as Python repr) → [BigInt digits, scale]
    let s = String(Number(x));
    if (/e/i.test(s)) s = Number(x).toFixed(10);
    let neg = s.startsWith('-'); if (neg) s = s.slice(1);
    const [i, f = ''] = s.split('.');
    return [BigInt((neg ? '-' : '') + (i + f).replace(/^0+(?=\d)/, '')), f.length];
}
function calcPrice(price_per_kg, weight_g) {
    const p = Number(price_per_kg) || 0, w = Number(weight_g) || 0;
    if (p <= 0 || w <= 0) return 0;
    const [pd, ps] = _decParts(p), [wd, ws] = _decParts(w);
    // amount = pd*wd / 10^(ps+ws) / 1000  → in paise: pd*wd*100 / 10^(ps+ws+3)
    const num = pd * wd * 100n, den = 10n ** BigInt(ps + ws + 3);
    const paise = (num * 2n + den) / (2n * den);          // half-up for positive values
    return Number(paise) / 100;
}
/** HTML-escape user-entered text (customer / product names) before innerHTML. */
function _h(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
/** Package line: same rule as backend (qty summed, rounded to 4dp, → grams to 1dp). */
function calcPackagePrice(qty_kg, price_per_kg) {
    const q = parseFloat((Number(qty_kg) || 0).toFixed(4));
    return calcPrice(price_per_kg, parseFloat((q * 1000).toFixed(1)));
}

function clearCart() {
    if (!cart.length) return;
    if (!confirm('Clear all items?')) return;
    cart = [];
    currentBill = null;
    renderCart();
    focusSearch();
}

// ════════════════════════════════════════════════════
// RENDER CART
// ════════════════════════════════════════════════════
function renderCart() {
    if (!cart.length) _clearBillSaveState();   // new cart session → new bill identity
    const listEl   = document.getElementById('cart-list');
    const footerEl = document.getElementById('bill-footer');
    const countEl  = document.getElementById('bill-item-count');
    const totalEl  = document.getElementById('bill-total');

    if (!cart.length) {
        listEl.innerHTML = `<div class="empty-cart"><div class="empty-cart-icon">🛒</div><p>No items yet</p><span>Select a product and press <kbd>Enter</kbd></span></div>`;
        const _chip = document.getElementById('bill-item-chip');
        if (_chip) _chip.textContent = '0';
        // Reset credit section and payment method on cart clear
        const _cds = document.getElementById('credit-due-section');
        if (_cds) _cds.style.display = 'none';
        const _pai = document.getElementById('paid-amount-input');
        if (_pai) _pai.value = '';
        _currentPaymentMethod = 'cash';
        ['cash','upi','credit'].forEach(m => {
            const btn = document.getElementById('pm-' + m);
            if (btn) btn.classList.toggle('active', m === 'cash');
        });
        footerEl.style.display = 'none';
        const _actBar = document.getElementById('bill-actions-bar');
        if (_actBar) _actBar.style.display = 'none';
        return;
    }
    footerEl.style.display = 'flex';
    const _actBar = document.getElementById('bill-actions-bar');
    if (_actBar) _actBar.style.display = 'flex';
    countEl.textContent = `${cart.length} item${cart.length>1?'s':''}`;
    const _chip = document.getElementById('bill-item-chip');
    if (_chip) _chip.textContent = cart.length;
    // NOTE: totalEl is updated by updateTotals() — no manual assignment here

    listEl.innerHTML = cart.map((item, cartIdx) => {
        // ── Package cart item ──
        if (item.isPackage) {
            const {pkg, qty_kg, activeItems, price} = item;
            const itemRows = activeItems.map(it => {
                return `<div style="font-size:11px;color:#555;padding:1px 0;">
                   • ${it.name_tamil || it.name_ta}${it.name_english || it.name_en ? ` <span style="color:#999;font-size:10px;">(${it.name_english || it.name_en})</span>` : ''}
                 </div>`;
            }).join('');
            return `
            <div class="cart-item cart-item-pkg">
              <div class="ci-top">
                <div style="flex:1;">
                  <div class="ci-name">${pkg.emoji} <strong>${pkg.name_english || pkg.name_en}</strong> <span style="font-size:11px;background:#ede9fe;color:#7c3aed;border-radius:4px;padding:1px 6px;">Bundle</span></div>
                  <div class="ci-price-per" style="color:#7c3aed;">${qty_kg.toFixed(2)} kg total</div>
                  <div class="pkg-cart-items" style="margin-top:6px;border-top:1px dashed #e0d9f7;padding-top:4px;">
                    ${itemRows}
                  </div>
                </div>
                <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">
                  <div class="ci-total"><span class="currency">₹</span><span class="value">${price.toFixed(2)}</span></div>
                  <button class="btn-ci-remove" onclick="removePackageFromCart(${cartIdx})">🗑</button>
                </div>
              </div>
            </div>`;
        }
        // ── Regular cart item ──
        const {product, weight_g, price, temp_price_per_kg} = item;
        const wtLabel    = weight_g>=1000 ? `${(weight_g/1000).toFixed(2)} kg` : `${weight_g} g`;
        const pricePerKg = _getCartItemPricePerKg(item);
        const tempBadge  = temp_price_per_kg!==null
            ? `<span class="temp-badge"><span class="currency">₹</span><span class="value">${temp_price_per_kg}/kg</span> temp</span>` : '';
        return `
        <div class="cart-item">
          <span class="cart-item-inline-name" title="${product.displayName}">${product.emoji||'🌿'} ${product.displayName} ${tempBadge}</span>
          <div class="ci-controls">
            <button class="ci-wt-btn minus" onclick="adjustQty('${product.id}',-${STEP_G})">−</button>
            <input type="number" class="ci-wt-input" value="${weight_g >= 1000 ? (weight_g/1000).toFixed(2) : weight_g}"
              min="1" step="${weight_g >= 1000 ? 0.05 : STEP_G}"
              data-unit="${weight_g >= 1000 ? 'kg' : 'g'}"
              data-pid="${product.id}"
              onchange="handleDirectQtyInput(this)"
              style="width:55px;text-align:center;padding:2px 4px;border:1.5px solid var(--border);border-radius:5px;font-size:12px;font-weight:700;font-family:var(--font);height:24px;" />
            <span style="font-size:10px;color:var(--muted);margin-left:-4px;">${weight_g >= 1000 ? 'kg' : 'g'}</span>
            <button class="ci-wt-btn plus" onclick="adjustQty('${product.id}',${STEP_G})">+</button>
          </div>
          <span class="cart-item-inline-price">₹${price.toFixed(2)}</span>
          <button class="cart-item-inline-remove" onclick="removeFromCart('${product.id}')">✕</button>
        </div>`;
    }).join('');
    calcBalance();
    // Refresh loyalty panel so Apply button shows/hides based on current cart total
    _refreshLoyaltyApplyBtn();

    // Auto-focus nudge: when first item lands in cart and customer name is empty,
    // show a gentle non-stealing hint toward the customer section (F6)
    if (cart.length === 1) {
        const nameVal = document.getElementById('customer-name')?.value.trim() || '';
        const focused = document.activeElement?.id;
        if (!nameVal && focused !== 'customer-name' && focused !== 'customer-phone') {
            setTimeout(() => showToast('💡 F6 → add customer  ·  F7 → payment'), 900);
        }
    }
}

// ════════════════════════════════════════════════════
// SINGLE SOURCE OF TRUTH — call on every state change
// ════════════════════════════════════════════════════
function updateTotals() {
    // ── 1. TOTAL = cart sum (with loyalty discount / server override) ──────────
    const cartSum = cart.reduce((s, i) => s + i.price, 0);
    const serverTotal = (window.currentBill && window.currentBill.total_amount != null)
        ? parseFloat(window.currentBill.total_amount) : null;
    const loyaltyDiscount = applyLoyaltyReward ? (_loyaltySettings.discount || 0) : 0;
    const total = serverTotal !== null ? serverTotal : Math.max(0, cartSum - loyaltyDiscount);

    // Update TOTAL display
    const totalEl = document.getElementById('bill-total');
    if (totalEl) totalEl.innerHTML = `<span class="currency">₹</span><span class="value">${total.toFixed(2)}</span>`;

    // ── Loyalty discount sidebar rows ──────────────────────────────────────────
    const discRow  = document.getElementById('loyalty-discount-row');
    const discLine = document.getElementById('loyalty-discount-line');
    const subAmt   = document.getElementById('bill-subtotal-amt');
    const discAmt  = document.getElementById('bill-discount-amt');
    if (loyaltyDiscount > 0) {
        if (discRow)  { discRow.style.display  = 'flex'; }
        if (discLine) { discLine.style.display = 'flex'; }
        if (subAmt)   subAmt.textContent  = `₹${cartSum.toFixed(2)}`;
        if (discAmt)  discAmt.textContent = `−₹${loyaltyDiscount.toFixed(2)}`;
    } else {
        if (discRow)  discRow.style.display  = 'none';
        if (discLine) discLine.style.display = 'none';
    }

    // ── 2. PAID = one source depending on active mode ──────────────────────────
    let paid = 0;
    if (_currentPaymentMethod === 'credit') {
        paid = Math.max(0, parseFloat(document.getElementById('paid-amount-input')?.value) || 0);
    } else {
        paid = Math.max(0, parseFloat(document.getElementById('cash-input')?.value) || 0);
    }

    // ── 3. DUE / BALANCE display ───────────────────────────────────────────────
    if (_currentPaymentMethod === 'credit') {
        // Credit mode: show due panel
        const due    = Math.max(0, total - paid);
        const dueRow = document.getElementById('due-row');
        const dueAmt = document.getElementById('due-amt');
        if (dueRow && dueAmt) {
            dueRow.style.display = 'flex';
            dueAmt.textContent   = `₹${due.toFixed(2)}`;
        }
        // Hide cash balance row in credit mode
        const balRow = document.getElementById('balance-row');
        if (balRow) balRow.style.display = 'none';
    } else {
        // Cash / UPI mode: show change or remaining due in balance row
        const balRow = document.getElementById('balance-row');
        const balAmt = document.getElementById('balance-amt');
        if (balRow && balAmt) {
            if (paid > 0) {
                const balance = paid - total;
                balRow.style.display = 'flex';
                if (balance >= 0) {
                    balAmt.innerHTML   = `Change: <span class="currency">₹</span><span class="value">${balance.toFixed(2)}</span>`;
                    balAmt.style.color = 'var(--green)';
                } else {
                    balAmt.innerHTML   = `Due: <span class="currency">₹</span><span class="value">${Math.abs(balance).toFixed(2)}</span>`;
                    balAmt.style.color = 'var(--red)';
                }
            } else {
                balRow.style.display = 'none';
            }
        }
        // Hide credit due row in cash/upi mode
        const dueRow = document.getElementById('due-row');
        if (dueRow) dueRow.style.display = 'none';
    }
}

// Backward-compat aliases — everything funnels to updateTotals()
function calcBalance() { updateTotals(); }
function calcDue()     { updateTotals(); }

// ════════════════════════════════════════════════════
// PRINT BILL — server-formatted 32-col thermal receipt (single source of truth)
// ════════════════════════════════════════════════════

function _escapeReceiptHtml(text) {
    return String(text || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

/** Fetch pre-formatted receipt lines from receipt_formatter.py (no client layout). */
async function fetchReceiptPreviewText(billId, paidAmount) {
    if (!billId) return '';
    const lang = _resolveBillLang();
    const btype = window.currentBillType || 'normal';
    const body = { bill_id: billId, language: lang, bill_type: btype };
    if (paidAmount != null && !isNaN(parseFloat(paidAmount))) {
        body.paid_amount = parseFloat(paidAmount);
    }
    const res = await fetch('/api/printer/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!data.success) throw new Error(data.message || 'Receipt preview failed');
    return data.preview || (Array.isArray(data.formatted_lines) ? data.formatted_lines.join('\n') : '');
}

/** On-screen / browser-print fallback uses same monospace text as thermal/PDF. */
async function renderPrintAreaFromServer(billId, paidAmount) {
    const pa = document.getElementById('print-area');
    if (!pa) return '';
    try {
        const text = await fetchReceiptPreviewText(billId, paidAmount);
        pa.innerHTML = `<pre class="thermal-receipt-pre">${_escapeReceiptHtml(text)}</pre>`;
        return text;
    } catch (e) {
        console.warn('Receipt preview fetch failed:', e);
        pa.innerHTML = '';
        return '';
    }
}

/** @deprecated — client-side HTML receipt removed; use renderPrintAreaFromServer. */
function buildThermalHTML() {
    return '';
}

// ── ISSUE 5: Reset bill number ──────────────────────────────────────────────
async function adminResetBillNumber() {
    const startVal = parseInt(document.getElementById('reset-bill-start')?.value || '1', 10);
    if (isNaN(startVal) || startVal < 1) { showToast('⚠️ Enter a valid start number (≥ 1)'); return; }
    if (!confirm(`Reset bill numbering to start from ${startVal}?\nThis affects future bills only — existing bills are NOT changed.`)) return;
    try {
        const r = await fetch(`${API}/admin/reset-bill-number`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ start_from: startVal })
        });
        const d = await r.json();
        if (d.success) { showToast(`✅ Bill number reset — next bill will be #${d.actual_next || startVal}`); }
        else { showToast('❌ Reset failed: ' + (d.message || 'unknown error')); }
    } catch(e) { showToast('❌ Server error: ' + e.message); }
}

// ── saveBillToServer — save bill and return the bill object ──────────────────
// Used by the optional Save button. Does NOT clear the cart.
async function saveBillToServer() {
    return await _saveBillCore({ silent: false });
}

// ── handlePrint — full workflow: save → stars → receipt → print ───────────────
let isPrinting = false;
let _printKeepsCart = true;   // v-patch: printing does NOT auto-clear the cart
let _pendingBrowserPrintBillId = null;

function resetBillingState({ showPrintedToast = false } = {}) {
    // Equivalent to New Bill shortcut behavior, plus a few UI cleanups.
    _clearBillSaveState();
    cart = [];
    currentBill = null;
    window.currentBill = null;
    renderCart();

    const phoneEl = document.getElementById('customer-phone');
    const nameEl  = document.getElementById('customer-name');
    const nameTa  = document.getElementById('customer-name-ta');
    const cashEl  = document.getElementById('cash-input');
    const paidEl  = document.getElementById('paid-amount-input');

    if (phoneEl) phoneEl.value = '';
    if (nameEl)  nameEl.value  = '';
    if (nameTa)  nameTa.value  = '';
    if (cashEl)  cashEl.value  = '';
    if (paidEl)  paidEl.value  = '';

    // Reset payment UI
    try {
        const section = document.getElementById('credit-due-section');
        if (section) section.style.display = 'none';
        const dueRow = document.getElementById('due-row');
        if (dueRow) dueRow.style.display = 'none';
        _currentPaymentMethod = 'cash';
        ['cash','upi','credit'].forEach(m => {
            const btn = document.getElementById('pm-' + m);
            if (btn) btn.classList.toggle('active', m === 'cash');
        });
        updateTotals();
    } catch {}

    // Reset loyalty / repeat-bill state
    _nameAutoFilled    = false;
    applyLoyaltyReward = false;
    _hideLoyaltyPanel();
    _latestRepeatBill = null;
    _renderRepeatBillPreview(null);
    _showCustomerDueWarning(0);

    // Reset bill type back to Normal (estimate should not carry over)
    try { setBillType('normal'); } catch {}

    if (showPrintedToast) showToast('✅ Bill sent to printer');
    fetchProducts(); // refresh stock counts
    focusSearch();
}

function _afterSuccessfulPrint() {
    // Brief success feedback then hard reset into next bill.
    // (The printer accepted the job; paper-out/cover-open cannot be detected.)
    showToast('✅ Bill sent to printer');
    setTimeout(() => resetBillingState(), 900);
}

// Listen for browser-print completion from /print-bill popup window
window.addEventListener('message', (ev) => {
    const d = ev && ev.data;
    if (!d || d.type !== 'POS_PRINT_DONE') return;
    if (!_pendingBrowserPrintBillId) return;
    if (String(d.bill_id) !== String(_pendingBrowserPrintBillId)) return;
    _pendingBrowserPrintBillId = null;
    _afterSuccessfulPrint();
});

async function handlePrint() {
    if (isPrinting) return;
    if (!cart.length) { showToast('⚠️ Cart is empty'); return; }
    isPrinting = true;

    // Step 1: save fresh on every Print (every Print = one DB record)
    const bill = await _saveBillCore({ silent: false });
    if (!bill) { isPrinting = false; return; }

    // Step 2: update loyalty star display with server-confirmed data
    if (bill.stars !== undefined) {
        updateStarDisplay(bill.stars, bill.loyalty ? bill.loyalty.stars_required : 10);
    }
    _updateLoyaltyAfterSave(bill);

    // Step 3: render receipt
    window.currentBill = bill;

    // Task 7: save last printed bill for R-key repeat
    window.lastBill = { cart: JSON.parse(JSON.stringify(cart)) };
    const _cashEl  = document.getElementById('cash-input');
    const _paidEl  = document.getElementById('paid-amount-input');
    const _enteredPaid = (_currentPaymentMethod === 'credit')
        ? Math.max(0, Number(_paidEl?.value) || 0)
        : Math.max(0, Number(_cashEl?.value) || 0);
    const _billTotal    = parseFloat(bill.total_amount != null ? bill.total_amount : bill.total) || 0;
    // AUDIT FIX F4: only cash tendered on a fully-paid bill is passed (to show change).
    // Credit / part-paid bills print the SAVED paid + due amounts from the server.
    const _isCredit     = (parseFloat(bill.due_amount) || 0) > 0;
    const _paidForPrint = (!_isCredit && _enteredPaid > _billTotal) ? _enteredPaid : null;

    await renderPrintAreaFromServer(bill.id, _paidForPrint);

    setTimeout(async () => {
        try {
            if (bill && bill.id) {
                const printed = await directThermalPrint(bill.id, _paidForPrint);
                if (printed) {
                    _afterSuccessfulPrint();
                } else if ((window._lastPrintMode || (_printerSettings.mode || 'escpos')) === 'escpos') {
                    // Bill IS saved; only printing failed. Cart is kept so Print can be
                    // pressed again — that reprints the SAME bill (no duplicate).
                    showToast(`⚠️ Bill #${bill.id} is SAVED but did not print. Fix the printer and press Print again (same bill will be reprinted).`);
                } else if ((window._lastPrintMode || (_printerSettings.mode || 'escpos')) === 'browser') {
                    // Wait for popup to notify print completion
                    _pendingBrowserPrintBillId = String(bill.id);
                }
            }
        } catch (e) {
            console.error('Print error:', e);
            showToast('Print failed: ' + (e.message || e));
        }
        isPrinting = false;
    }, 100);
}

// Keep legacy name intact — onclick="printBill()" in HTML still works
function printBill() { handlePrint(); }

// ── _resetAfterPrint — full POS reset; called ONLY by clearCart() path ────────
function _resetAfterPrint() {
    resetBillingState();
}

// ════════════════════════════════════════════════════
// SAVE BILL — core engine + public wrappers
// ════════════════════════════════════════════════════

// saveBill — called by the Save button (onclick="saveBill()").
// Saves, updates star display + loyalty panel, then clears the cart.
async function saveBill() {
    const bill = await _saveBillCore({ silent: false });
    if (!bill) return;

    // Update loyalty display
    if (bill.stars !== undefined) {
        updateStarDisplay(bill.stars, bill.loyalty ? bill.loyalty.stars_required : 10);
    }
    _updateLoyaltyAfterSave(bill);

    // Clear cart and fields — loyalty panel stays visible for this customer
    cart = []; renderCart();
    document.getElementById('cash-input').value     = '';
    document.getElementById('customer-name').value  = '';
    document.getElementById('customer-phone').value = '';
    _nameAutoFilled    = false;
    applyLoyaltyReward = false;
    fetchProducts();
    focusSearch();
}

// PART 2 — Show confirmed loyalty result inside bill panel after save
function _updateLoyaltyAfterSave(bill) {
    const panel = document.getElementById('loyalty-panel');
    if (!panel) return;

    // Respect live enabled flag from bill response; hide panel if disabled
    const _enabled = (bill.loyalty_enabled !== undefined) ? bill.loyalty_enabled : _loyaltySettings.enabled;
    if (!_enabled) { panel.style.display = 'none'; return; }

    // Prefer top-level bill fields (now always present); fall back to loyalty sub-obj
    const loyalty      = bill.loyalty || {};
    const stars        = (bill.stars        !== undefined) ? bill.stars        : (loyalty.stars_after    || 0);
    const starsFor     = (bill.stars_required !== undefined) ? bill.stars_required : (loyalty.stars_required || _loyaltySettings.stars_required || 5);
    const rewardAmt    = (bill.reward_amount  !== undefined) ? bill.reward_amount  : (_loyaltySettings.discount || 100);
    const discount     = bill.discount_applied || bill.loyalty_discount || 0;
    const starsEarned  = loyalty.stars_earned  || 0;
    const rewardApplied = loyalty.reward_applied || discount > 0;
    const remaining    = starsFor - stars;

    const hintText = remaining > 0
        ? `Earn ${remaining} more star${remaining > 1 ? 's' : ''} to get \u20B9${rewardAmt} discount`
        : `\uD83C\uDF89 Reward Ready! \u20B9${rewardAmt} discount available`;

    let html = `
      <div class="row">
        <span>\u2B50 Loyalty</span>
        <span></span>
        <span>${stars} / ${starsFor}</span>
      </div>
      <div class="row small-text">
        <span>${hintText}</span>
        <span></span>
        <span></span>
      </div>`;

    if (starsEarned > 0 && !rewardApplied) {
        html += `<div class="row small-text"><span style="color:#28a745;font-weight:700;">+${starsEarned} \u2B50 earned this bill!</span><span></span><span></span></div>`;
    }
    if (rewardApplied) {
        html += `<div class="row"><span>\uD83C\uDF81 Reward Applied</span><span></span><span>-\u20B9${discount}</span></div>`;
    }

    panel.innerHTML     = html;
    panel.style.display = 'block';
}


async function loadAnalytics() {
    try {
        const spinner = '<i class="fas fa-spinner fa-spin" style="color:var(--text-light);font-size:0.8em"></i>';
        document.getElementById('stat-today-rev').innerHTML = spinner;
        document.getElementById('stat-today-bills').innerHTML = spinner;
        document.getElementById('stat-total-rev').innerHTML = spinner;
        document.getElementById('stat-total-bills').innerHTML = spinner;
        document.getElementById('stat-best').innerHTML = spinner;


        const [sumR, billsR] = await Promise.all([
            fetch(`${API}/summary`), fetch(`${API}/bills`)
        ]);
        const sum   = await sumR.json();
        const bdata = await billsR.json();

        document.getElementById('stat-today-rev').innerHTML   = `<span class="currency">₹</span><span class="value">${sum.today_revenue.toFixed(2)}</span>`;
        document.getElementById('stat-today-bills').textContent = sum.today_bills;
        document.getElementById('stat-total-rev').innerHTML   = `<span class="currency">₹</span><span class="value">${sum.total_revenue.toFixed(2)}</span>`;
        document.getElementById('stat-total-bills').textContent = sum.total_bills;
        document.getElementById('stat-best').textContent        = sum.most_sold_product || '—';

        // Top products
        const entries = Object.entries(sum.product_sales||{})
            .sort((a,b) => b[1].revenue-a[1].revenue);
        document.getElementById('breakdown-list').innerHTML = entries.length
            ? entries.map(([name,data],i) => `
                <div class="breakdown-row">
                  <div class="br-rank">${i+1}</div>
                  <div class="br-name">${name}</div>
                  <div class="br-stats">
                    <div class="br-revenue"><span class="currency">₹</span><span class="value">${data.revenue.toFixed(2)}</span></div>
                    <div>${data.weight_kg.toFixed(2)} kg · ${data.qty} bills</div>
                  </div>
                </div>`).join('')
            : '<p style="color:#888;padding:10px;font-size:13px;">No sales yet</p>';

        // Recent bills
        const bills = (bdata.bills||[]).slice(0,30);
        document.getElementById('recent-bills-list').innerHTML = bills.length
            ? bills.map(b => `
                <div class="recent-bill-row" onclick="openBillDetail('${b.id}')">
                  <div>
                    <span class="rbill-id">Bill #${b.id}</span>
                    <span class="rbill-name">${_h(b.customer_name||'Walk-in')}</span>
                  </div>
                  <div>
                    <span class="rbill-total"><span class="currency">₹</span><span class="value">${parseFloat(b.total).toFixed(2)}</span></span>
                    <span class="rbill-date">${b.created_at}</span>
                  </div>
                </div>`).join('')
            : '<p style="color:#888;padding:10px;font-size:13px;">No bills yet</p>';

        // Store bills for detail view
        window._allBills = bdata.bills || [];

        // Show/hide cutoff notice banner
        const banner = document.getElementById('analytics-cutoff-banner');
        if (banner) {
            banner.style.display = sum.analytics_cutoff_active ? 'flex' : 'none';
        }
    } catch { document.getElementById('stat-today-rev').textContent = 'Offline'; }
}

// ════════════════════════════════════════════════════════════════
// CLEAR ANALYTICS — modal + API call
// ════════════════════════════════════════════════════════════════

let _caMode = 'today';  // current mode selection in modal

function showClearAnalyticsModal() {
    _caMode = 'today';
    setCaMode('today');  // reset visual state
    document.getElementById('ca-clear-bills-chk').checked = false;
    _updateCaBillsHint();
    document.getElementById('clear-analytics-overlay').classList.add('open');
    document.getElementById('clear-analytics-modal').classList.add('open');
}

function closeClearAnalyticsModal() {
    document.getElementById('clear-analytics-overlay').classList.remove('open');
    document.getElementById('clear-analytics-modal').classList.remove('open');
}

function setCaMode(mode) {
    _caMode = mode;
    document.getElementById('ca-mode-today').classList.toggle('ca-mode-active', mode === 'today');
    document.getElementById('ca-mode-all').classList.toggle('ca-mode-active', mode === 'all');
    _updateCaBillsHint();
}

function _updateCaBillsHint() {
    const chk  = document.getElementById('ca-clear-bills-chk');
    const hint = document.getElementById('ca-bills-hint');
    if (!hint) return;
    if (chk && chk.checked) {
        const label = _caMode === 'today' ? "today's" : 'ALL';
        hint.textContent = `⚠️ Will permanently DELETE ${label} bill records from the database. Cannot be undone.`;
        hint.style.color = '#dc2626';
    } else {
        hint.textContent = 'When unchecked: bills are preserved, only the analytics view resets.';
        hint.style.color = '';
    }
}

// Wire up checkbox change event (runs once DOM is ready)
document.addEventListener('DOMContentLoaded', () => {
    const chk = document.getElementById('ca-clear-bills-chk');
    if (chk) chk.addEventListener('change', _updateCaBillsHint);
});

async function executeClearAnalytics() {
    const clearBills = document.getElementById('ca-clear-bills-chk').checked;
    const confirmBtn = document.getElementById('ca-confirm-btn');

    // Extra guard when deleting real data
    if (clearBills) {
        const scope = _caMode === 'today' ? "today's bills" : 'ALL bill records';
        const ok = confirm(
            `⚠️ FINAL WARNING\n\nYou are about to permanently DELETE ${scope}.\n` +
            `This cannot be undone.\n\nType OK to confirm.`
        );
        if (!ok) return;
    }

    confirmBtn.disabled = true;
    confirmBtn.textContent = '⏳ Clearing…';

    try {
        const res = await fetch(`${API}/api/admin/clear-analytics`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ mode: _caMode, clear_bills: clearBills }),
        });
        const data = await res.json();

        if (data.success) {
            closeClearAnalyticsModal();
            showToast(`✅ ${data.message}`);
            // Refresh the analytics dashboard immediately
            await loadAnalytics();
        } else {
            showToast(`❌ ${data.message || 'Clear failed'}`);
        }
    } catch (e) {
        showToast('⚠️ Server offline or request failed');
    } finally {
        confirmBtn.disabled = false;
        confirmBtn.textContent = '🗑️ Clear Data';
    }
}

async function restoreFullAnalytics() {
    // Remove the analytics cutoff by clearing with mode=all, clear_bills=false
    // But in this case we want to remove the cutoff key entirely — send a special payload
    try {
        const res = await fetch(`${API}/api/admin/clear-analytics`, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify({ mode: 'restore', clear_bills: false }),
        });
        // If server doesn't understand 'restore', it will 400; handle gracefully
        // We call the endpoint; if it errors, we reload anyway
    } catch (_) {}
    // Force reload
    showToast('🔄 Restoring full analytics view…');
    await loadAnalytics();
}

function openBillDetail(billId) {
    const bill = (window._allBills||[]).find(b => b.id===billId);
    if (!bill) return;
    window._detailBillId = billId;
    document.getElementById('bill-detail-title').textContent =
        `Bill #${bill.id}  –  ${bill.customer_name||'Walk-in'}`;
    const items = (typeof bill.items==='string') ? JSON.parse(bill.items) : bill.items||[];
    document.getElementById('bill-detail-body').innerHTML = `
      <div style="font-size:12px;color:#888;margin-bottom:10px;">
        ${bill.created_at} · ${bill.customer_phone||''}
      </div>
      <table class="detail-table">
        <thead><tr><th>Item</th><th>Qty</th><th>Rate</th><th>Total</th></tr></thead>
        <tbody>
          ${items.map(it=>`
            <tr>
              <td>${_h(it.name)}</td>
              <td>${it.weight_g>=1000?(it.weight_g/1000).toFixed(2)+'kg':it.weight_g+'g'}</td>
              <td><span class="currency">₹</span><span class="value">${it.price_per_kg}/kg</span></td>
              <td><span class="currency">₹</span><span class="value">${parseFloat(it.total_price).toFixed(2)}</span></td>
            </tr>`).join('')}
        </tbody>
      </table>
      <div class="detail-total">TOTAL: <span class="currency">₹</span><span class="value">${parseFloat(bill.total).toFixed(2)}</span></div>
      ${bill.cash>0 ? `<div class="detail-sub">Cash: <span class="currency">₹</span><span class="value">${parseFloat(bill.cash).toFixed(2)}</span> · Balance: <span class="currency">₹</span><span class="value">${parseFloat(bill.balance).toFixed(2)}</span></div>` : ''}`;
    document.getElementById('bill-detail-overlay').classList.add('open');
    document.getElementById('bill-detail-modal').classList.add('open');
}

function closeBillDetail() {
    document.getElementById('bill-detail-overlay').classList.remove('open');
    document.getElementById('bill-detail-modal').classList.remove('open');
}

// ════════════════════════════════════════════════════
// ADMIN – STOCK MANAGEMENT
// ════════════════════════════════════════════════════
function renderStockList() {
    const q   = (document.getElementById('stock-search')?.value || '').toLowerCase();
    const el  = document.getElementById('stock-product-list');
    if (!el) return;
    const list = products.filter(p => {
        const st = Array.isArray(p.search_terms) ? p.search_terms.join(' ') : (p.search_terms || '');
        const corpus = [p.displayName, p.search_tags, st].filter(Boolean).join(' ').toLowerCase();
        return corpus.includes(q);
    }).sort((a, b) => {
        const sa = (a.serial_no === null || a.serial_no === undefined || a.serial_no === '') ? Infinity : Number(a.serial_no);
        const sb = (b.serial_no === null || b.serial_no === undefined || b.serial_no === '') ? Infinity : Number(b.serial_no);
        if (sa !== sb) return sa - sb;
        return (a.displayName || '').localeCompare(b.displayName || '');
    });
    if (!list.length) { el.innerHTML = '<p style="color:#888;padding:10px;">No products found</p>'; return; }

    el.innerHTML = `
      <table class="detail-table stock-table">
        <thead>
          <tr>
            <th>Product</th>
            <th>Current Stock</th>
            <th>Status</th>
            <th>Set Stock (kg)</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${list.map(p => {
              const st = (p.stock !== null && p.stock !== undefined) ? parseFloat(p.stock) : null;
              let statusHtml = '<span class="stock-status-na">Not tracked</span>';
              if (st !== null) {
                  if (st <= 0)  statusHtml = '<span class="adm-stock-out">⛔ Out of Stock</span>';
                  else if (st < 5) statusHtml = `<span class="adm-stock-low">⚠️ Low Stock</span>`;
                  else          statusHtml = '<span class="adm-stock-ok">✅ In Stock</span>';
              }
              const curStock = st !== null ? st.toFixed(2) : '—';
              return `<tr>
                <td><strong>${p.emoji||'🌿'} ${p.displayName}</strong></td>
                <td>${curStock} ${st !== null ? 'kg' : ''}</td>
                <td>${statusHtml}</td>
                <td><input type="number" id="stk-${p.id}" min="0" step="0.01"
                     value="${st !== null ? st : ''}"
                     placeholder="kg (blank=off)"
                     style="width:100px;padding:5px 8px;border:1px solid #ddd;border-radius:5px;font-size:13px;"/></td>
                <td><button onclick="updateStock('${p.id}')"
                     style="padding:5px 12px;background:var(--green);color:#fff;border:none;border-radius:5px;cursor:pointer;font-size:12px;font-weight:700;">
                     💾 Save</button></td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>`;
}

async function updateStock(pid) {
    const input  = document.getElementById(`stk-${pid}`);
    if (!input) return;
    const rawVal = input.value.trim();
    const stockKg = rawVal === '' ? null : parseFloat(rawVal);
    try {
        const r = await fetch(`${API}/admin/products/${pid}/stock`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({stock: isNaN(stockKg) ? null : stockKg})
        });
        const d = await r.json();
        if (d.success) {
            showToast('✅ Stock updated!');
            // Reload from server so Stock tab always reflects latest DB value
            await fetchProducts();
            renderStockList();
        } else { showToast('❌ Failed to update stock'); }
    } catch { showToast('⚠️ Server offline'); }
}

// ════════════════════════════════════════════════════
// ADMIN – PRODUCTS
// ════════════════════════════════════════════════════
function renderAdminList() {
    const q   = (document.getElementById('adm-search')?.value||'').toLowerCase();
    const el  = document.getElementById('admin-product-list');
    if (!el) return;
    const list = products.filter(p => {
        const st = Array.isArray(p.search_terms) ? p.search_terms.join(' ') : (p.search_terms || '');
        const corpus = [p.displayName, p.search_tags, st].filter(Boolean).join(' ').toLowerCase();
        return corpus.includes(q);
    }).sort((a, b) => {
        // Sort by S.NO (catalog order), then alphabetically by name
        const sa = (a.serial_no === null || a.serial_no === undefined || a.serial_no === '') ? Infinity : Number(a.serial_no);
        const sb = (b.serial_no === null || b.serial_no === undefined || b.serial_no === '') ? Infinity : Number(b.serial_no);
        if (sa !== sb) return sa - sb;
        return (a.displayName || '').localeCompare(b.displayName || '');
    });
    if (!list.length) { el.innerHTML='<p style="color:#888;padding:10px;">No products found</p>'; return; }
    el.innerHTML = list.map(p => {
        const imgHtml = p.image
            ? `<img src="images/${p.image}" class="adm-thumb" loading="lazy" onerror="this.style.display='none'"/>`
            : `<span class="adm-emoji">${p.emoji||'🌿'}</span>`;
        const st = (p.stock !== null && p.stock !== undefined) ? parseFloat(p.stock) : null;
        let stockLabel = '<span class="adm-stock-na">—</span>';
        if (st !== null) {
            if (st <= 0)  stockLabel = '<span class="adm-stock-out">⛔ OOS</span>';
            else if (st < 5) stockLabel = `<span class="adm-stock-low">⚠️ ${st.toFixed(1)} kg</span>`;
            else          stockLabel = `<span class="adm-stock-ok">📦 ${st.toFixed(1)} kg</span>`;
        }
        const sno = (p.serial_no !== null && p.serial_no !== undefined && p.serial_no !== '') ? p.serial_no : '—';
        return `<div class="adm-row" onclick="openEditModal('${p.id}')">
          <span class="adm-sno" title="Catalog S.NO">${sno}</span>
          ${imgHtml}
          <span class="adm-pname">${p.displayName}</span>
          <span class="adm-pprice">S:<span class="currency">₹</span><span class="value">${p.s_rate||p.price_per_kg}</span> / P:<span class="currency">₹</span><span class="value">${p.p_rate||'—'}</span> ${p.wholesale_rate ? '/ W:<span class="currency">₹</span><span class="value">'+p.wholesale_rate+'</span>' : ''}</span>
          ${stockLabel}
          <span class="adm-pcat">${p.category}</span>
        </div>`;
    }).join('');
}

function openEditModal(pid) {
    const p = products.find(x => x.id===pid);
    if (!p) return;
    document.getElementById('edit-pid').value      = pid;
    const editSnoEl = document.getElementById('edit-serial-no');
    if (editSnoEl) editSnoEl.value = (p.serial_no !== null && p.serial_no !== undefined) ? p.serial_no : '';
    document.getElementById('edit-name-en').value  = p.name_en || (p.name||'').split('(')[0].trim();
    document.getElementById('edit-name-ta').value  = p.name_tamil || p.name_ta || '';
    document.getElementById('edit-price').value    = p.price_per_kg;
    document.getElementById('edit-srate').value    = (p.s_rate != null) ? p.s_rate : '';
    document.getElementById('edit-prate').value    = (p.p_rate != null) ? p.p_rate : '';
    document.getElementById('edit-wholesale').value = (p.wholesale_rate != null) ? p.wholesale_rate : '';
    // Set category — try exact match first, fall back to setting value directly
    const catSel = document.getElementById('edit-cat');
    const catOpts = Array.from(catSel.options).map(o => o.value);
    if (catOpts.includes(p.category)) catSel.value = p.category;
    else { const opt = new Option(p.category, p.category); catSel.appendChild(opt); catSel.value = p.category; }
    document.getElementById('edit-emoji').value    = p.emoji||'🌾';
    document.getElementById('edit-tags').value     = p.search_tags||'';
    // ── Stock field ──
    const stEl = document.getElementById('edit-stock');
    stEl.value = (p.stock !== null && p.stock !== undefined) ? p.stock : '';
    const prev = document.getElementById('edit-img-preview');
    if (p.image) { prev.src=`images/${p.image}`; prev.style.display=''; }
    else          { prev.style.display='none'; }
    // Load nutrient values
    const nuts = (typeof p.nutrients === 'string') ? JSON.parse(p.nutrients || '{}') : (p.nutrients || {});
    document.getElementById('edit-nut-protein').value = (nuts.protein != null) ? nuts.protein : '';
    document.getElementById('edit-nut-abs-protein').value = (nuts.absorbable_protein != null) ? nuts.absorbable_protein : '';
    document.getElementById('edit-nut-fat').value = (nuts.fat != null) ? nuts.fat : '';
    document.getElementById('edit-nut-carbs').value = (nuts.carbohydrates != null) ? nuts.carbohydrates : '';
    document.getElementById('edit-nut-fiber').value = (nuts.fiber != null) ? nuts.fiber : '';
    document.getElementById('edit-nut-moisture').value = (nuts.moisture != null) ? nuts.moisture : '';
    document.getElementById('edit-nut-ash').value = (nuts.ash != null) ? nuts.ash : '';
    document.getElementById('edit-overlay').classList.add('open');
    document.getElementById('edit-modal').classList.add('open');
}

function closeEditModal() {
    document.getElementById('edit-overlay').classList.remove('open');
    document.getElementById('edit-modal').classList.remove('open');
}


function previewAddImage(ev) {
    const file = ev.target.files[0]; if (!file) return;
    const reader = new FileReader();
    reader.onload = e => {
        const prev = document.getElementById('adm-add-img-preview');
        prev.src = e.target.result; prev.style.display='';
    };
    reader.readAsDataURL(file);
}

function previewEditImage(ev) {
    const file = ev.target.files[0]; if (!file) return;
    const reader = new FileReader();
    reader.onload = e => {
        const prev = document.getElementById('edit-img-preview');
        prev.src = e.target.result; prev.style.display='';
    };
    reader.readAsDataURL(file);
}

async function adminSaveProduct() {
    const pid    = document.getElementById('edit-pid').value;
    const snoRaw = (document.getElementById('edit-serial-no')?.value || '').trim();
    const serialNo = snoRaw === '' ? null : parseInt(snoRaw, 10);
    const nameEn = document.getElementById('edit-name-en').value.trim();
    const nameTa = document.getElementById('edit-name-ta').value.trim();
    const sRate  = parseFloat(document.getElementById('edit-srate').value) || null;
    const pRate  = parseFloat(document.getElementById('edit-prate').value) || null;
    const wRate  = parseFloat(document.getElementById('edit-wholesale').value) || null;
    const price  = sRate || parseFloat(document.getElementById('edit-price').value) || 100;
    const cat    = document.getElementById('edit-cat').value;
    const emoji  = document.getElementById('edit-emoji').value.trim();
    const tags   = document.getElementById('edit-tags').value.trim();
    // Collect nutrient values for save
    const editNutrients = {};
    const eNutP = parseFloat(document.getElementById('edit-nut-protein').value);
    const eNutAP = parseFloat(document.getElementById('edit-nut-abs-protein').value);
    const eNutF = parseFloat(document.getElementById('edit-nut-fat').value);
    const eNutC = parseFloat(document.getElementById('edit-nut-carbs').value);
    const eNutFi = parseFloat(document.getElementById('edit-nut-fiber').value);
    const eNutM = parseFloat(document.getElementById('edit-nut-moisture').value);
    const eNutA = parseFloat(document.getElementById('edit-nut-ash').value);
    if (!isNaN(eNutP)) editNutrients.protein = eNutP;
    if (!isNaN(eNutAP)) editNutrients.absorbable_protein = eNutAP;
    if (!isNaN(eNutF)) editNutrients.fat = eNutF;
    if (!isNaN(eNutC)) editNutrients.carbohydrates = eNutC;
    if (!isNaN(eNutFi)) editNutrients.fiber = eNutFi;
    if (!isNaN(eNutM)) editNutrients.moisture = eNutM;
    if (!isNaN(eNutA)) editNutrients.ash = eNutA;
    if (!nameEn) { showToast('⚠️ English name required'); return; }
    if (!sRate && sRate !== 0) { showToast('⚠️ S.Rate (Selling rate) is required'); document.getElementById('edit-srate').focus(); return; }
    if (!pRate && pRate !== 0) { showToast('⚠️ P.Rate (Purchase rate) is required'); document.getElementById('edit-prate').focus(); return; }
    if (!wRate && wRate !== 0) { showToast('⚠️ Wholesale rate is required'); document.getElementById('edit-wholesale').focus(); return; }
    try {
        // Step 1: Save product details
        const saveResp = await fetch(`${API}/admin/products/${pid}`, {
            method:'PUT', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({name_english:nameEn, name_tamil:nameTa, name_en:nameEn, name_ta:nameTa,
                                  price_per_kg:price, price:price, category:cat, emoji, search_tags:tags,
                                  s_rate:sRate, p_rate:pRate, wholesale_rate:wRate, nutrients:editNutrients,
                                  serial_no: (serialNo !== null && !isNaN(serialNo)) ? serialNo : null})
        });
        if (!saveResp.ok) { showToast('❌ Save failed – server error'); return; }
        const saveData = await saveResp.json();
        if (!saveData.success) { showToast('❌ Save failed: ' + (saveData.message||'unknown')); return; }

        // Step 1b: Save stock separately
        const stockRaw = document.getElementById('edit-stock').value.trim();
        const stockVal = stockRaw === '' ? null : parseFloat(stockRaw);
        await fetch(`${API}/admin/products/${pid}/stock`, {
            method:'PUT', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({stock: isNaN(stockVal) ? null : stockVal})
        });

        // Step 2: Upload image if selected — wait for BOTH FileReader AND server upload
        const fi = document.getElementById('edit-image');
        if (fi && fi.files[0]) {
            showToast('⏳ Uploading image…');
            const originalFilename = fi.files[0].name;
            await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = async e => {
                    try {
                        await fetch(`${API}/admin/products/${pid}/image`, {
                            method:'POST', headers:{'Content-Type':'application/json'},
                            body: JSON.stringify({image_data: e.target.result, original_filename: originalFilename})
                        });
                        resolve();
                    } catch (err) { reject(err); }
                };
                reader.onerror = reject;
                reader.readAsDataURL(fi.files[0]);
            });
        }

        // Step 3: Only now close modal and refresh — image is fully saved
        showToast('✅ Product saved!');
        closeEditModal();
        await fetchProducts();
    } catch(e) { showToast('❌ Save failed: ' + e.message); }
}

async function adminDeleteProduct() {
    const pid = document.getElementById('edit-pid').value;
    if (!confirm('Delete this product?')) return;
    try {
        await fetch(`${API}/admin/products/${pid}`, {method:'DELETE'});
        showToast('🗑 Deleted'); closeEditModal();
        await fetchProducts();
    } catch { showToast('❌ Delete failed'); }
}

// ── English → Tamil auto-translate map (offline, instant) ──
const EN_TO_TAMIL = {
    // ── Millets ──
    "thinai":"தினை","sen thinai":"செந்தினை","foxtail millet":"தினை",
    "red foxtail millet":"செந்தினை","foxtail seeds":"தினை விதை",
    "pani varagu":"பனிவரகு","samai":"சாமை","little millet":"சாமை",
    "kambu":"நாட்டுகம்பு","pearl millet":"கம்பு",
    "kuthiraivali":"குதிரைவாலி","barnyard millet":"குதிரைவாலி",
    "varagu":"வரகு","kodo millet":"வரகு","korali":"கோராலி",
    "ragi":"கேழ்வரகு","finger millet":"கேழ்வரகு",
    "sorghum":"சோளம்","white sorghum":"வெள்ளை சோளம்",
    "red sorghum":"சிவப்பு சோளம்","dark sorghum":"கருப்பு சோளம்",
    "black millet":"கருப்பு கம்பு","green millet":"பச்சை கம்பு",
    "red millet":"சிவப்பு கம்பு","white millet":"வெள்ளை கம்பு",
    "yellow millet":"மஞ்சள் கம்பு","fox millet":"நரி கம்பு",
    "browntop millet":"ஊதா கம்பு",
    // ── Rice & Grains ──
    "rice":"அரிசி","red rice":"சிவப்பு அரிசி","black rice":"கருப்பு அரிசி",
    "bamboo rice":"மூங்கில் அரிசி","mappillai samba rice":"மாப்பிள்ளை சம்பா",
    "karunguruvai black rice":"கருங்குருவை","paddy":"நெல்","nel":"நெல்",
    "wheat":"கோதுமை","samba wheat":"சம்பா கோதுமை",
    "corn":"மக்கா சோலம்","maize":"மக்கா சோலம்","popcorn":"பாப்கார்ன்",
    "sweet corn":"இனிப்பு சோளம்",
    "oats":"ஓட்ஸ்","rolled oats":"ரோல்டு ஓட்ஸ்","long oats":"நீள ஓட்ஸ்",
    "barley":"பார்லி","quinoa":"கீனோவா","buckwheat":"பக்வீட்",
    "amaranth":"அமராந்த்",
    // ── Seeds ──
    "groundnut":"வேர்க்கடலை","peanut":"கோல்டன்கடலை",
    "sesame":"எள்ளு","white sesame":"வெள்ளை எள்ளு","black sesame":"கருப்பு எள்ளு",
    "sunflower":"சூரியகாந்தி","sunflower seed":"சூரியகாந்தி விதை",
    "mustard":"கடுகு","flax seed":"ஆளிவிதை","flaxseed":"ஆளிவிதை","flax":"ஆளிவிதை",
    "chia seed":"சியா விதை","chia":"சியா விதை",
    "pumpkin seed":"பூசனி விதை","pumpkin":"பூசனி விதை",
    "niger seed":"நைஜர் விதை","niger":"நைஜர் விதை",
    "safflower":"குசும்பா","watermelon seed":"தர்பூசணி விதை","watermelon":"தர்பூசணி",
    "poppy seed":"கசகசா","poppy seeds":"கசகசா",
    "cucumber seed":"வெள்ளரி விதை",
    "hemp seed":"ஹெம்ப் விதை","hemp":"ஹெம்ப்",
    "carom seed":"ஓமம்","dill seed":"சதகுப்பை விதை",
    "canary seed":"கேனரி விதை",
    // ── Nuts ──
    "almond":"பாதாம்","cashew":"முந்திரி","walnut":"அக்ரூட்",
    "pistachio":"பிஸ்தா","charoli":"சாரோலி",
    // ── Pulses / Legumes ──
    "green gram":"பச்சைபயிர்","moong":"பச்சைபயிர்",
    "black gram":"கருப்பு உளுந்து","black urad":"கருப்பு உளுந்து",
    "urad":"உளுந்து","beans":"அவரை","field beans":"அவரைக்காய்",
    "horse gram":"கொள்ளு","kollu":"கொள்ளு",
    "soya":"சோயா","soybean":"சோயா",
    "rajma":"ராஜ்மா","kidney beans":"ராஜ்மா",
    "red toor dal":"சிவப்பு துவரை","toor dal":"துவரை",
    "red lentil":"மைசூர் பருப்பு","lentil":"பருப்பு",
    "green peas":"பச்சை பட்டாணி",
    "chickpeas":"கொண்டக்கடலை","chickpea":"கொண்டக்கடலை",
    "red cow pea":"சிவப்பு தட்டபயிர்",
    "black eyed peas":"கராமணி",
    // ── Spices & Condiments ──
    "turmeric":"மஞ்சள்","kasturi turmeric":"கஸ்தூரி மஞ்சள்",
    "pepper":"மிளகு","black pepper":"மிளகு",
    "cumin":"சீரகம்","black cumin":"கருஞ்சீரகம்",
    "fenugreek":"வெந்தயம்","coriander":"தனியா",
    "cardamom":"ஏலக்காய்","cloves":"கிராம்பு",
    "cinnamon":"இலவங்கப்பட்டை","ginger":"இஞ்சி","garlic":"பூண்டு",
    "onion":"வெங்காயம்","salt":"உப்பு","bamboo salt":"மூங்கில் உப்பு",
    "sugar":"சர்க்கரை","olive":"ஆலிவ்",
    // ── Sweeteners & Dry fruits ──
    "jaggery":"வெல்லம்","honey":"தேன்",
    "dates":"பேரிச்சம்பழம்","black dates":"கருப்பு பேரீச்சை",
    "raisins":"திராட்சை","raisin":"திராட்சை",
    "coconut":"தேங்காய்",
    // ── Herbal / Special ──
    "moringa":"முருங்கை","moringa seed":"முருங்கை விதை",
    "ashwagandha":"அமுக்கிரா","alfalfa":"குதிரைமசால்",
    "costus root":"கொஸ்டஸ் வேர்","asparagus":"சதாவரி",
    "blue lotus":"நீலதாமரை","milk thistle":"பால் நெருஞ்சி",
    "banyan seed":"ஆல விதை","betel nut":"பாக்கு",
    "lime stone":"சுண்ணாம்பு","egg shell":"முட்டை ஓடு",
    // ── Oil ──
    "oil":"எண்ணெய்","groundnut oil":"வேர்க்கடலை எண்ணெய்",
    "sesame oil":"நல்லெண்ணெய்","coconut oil":"தேங்காய் எண்ணெய்",
    // ── Feed ──
    "bird feed":"பறவை தீவனம்","hamster pellet":"ஹம்ஸ்டர் பெல்லெட்",
    "rabbit pellet":"முயல் பெல்லெட்",
};

let _taAutoFilled = false;  // track if Tamil was auto-filled by English input

function autoTranslateToTamil() {
    const enVal = document.getElementById('adm-name-en').value.trim().toLowerCase();
    const taEl  = document.getElementById('adm-name-ta');

    // If English is cleared → clear Tamil only if it was auto-filled
    if (!enVal) {
        if (_taAutoFilled) {
            taEl.value    = '';
            _taAutoFilled = false;
        }
        return;
    }

    // Only auto-fill Tamil if field is empty OR was previously auto-filled
    if (taEl.value.trim() && !_taAutoFilled) return;

    const match = EN_TO_TAMIL[enVal];
    if (match) {
        taEl.value    = match;
        _taAutoFilled = true;
        taEl.style.background = '#e8f5e9';
        setTimeout(() => { taEl.style.background = ''; }, 1500);
    } else {
        // Try partial match: longest key first to avoid short-key collisions
        const sortedKeys = Object.keys(EN_TO_TAMIL).sort((a,b) => b.length - a.length);
        for (const k of sortedKeys) {
            if (enVal.includes(k)) {
                taEl.value = EN_TO_TAMIL[k];
                _taAutoFilled = true;
                taEl.style.background = '#fff3e0';
                setTimeout(() => { taEl.style.background = ''; }, 1500);
                return;
            }
        }
        // Try multi-word match: split and translate each word
        const words = enVal.split(/\s+/);
        if (words.length > 1) {
            const translated = words.map(w => EN_TO_TAMIL[w] || w).join(' ');
            if (translated !== enVal) {
                taEl.value = translated;
                _taAutoFilled = true;
                taEl.style.background = '#fff3e0';
                setTimeout(() => { taEl.style.background = ''; }, 1500);
                return;
            }
        }
        // No match found – clear previous auto-fill if any
        if (_taAutoFilled) {
            taEl.value    = '';
            _taAutoFilled = false;
        }
    }
}

async function adminAddProduct() {
    const nameEn = document.getElementById('adm-name-en').value.trim();
    const nameTa = document.getElementById('adm-name-ta').value.trim();
    const snoRaw = (document.getElementById('adm-serial-no')?.value || '').trim();
    const serialNo = snoRaw === '' ? null : parseInt(snoRaw, 10);
    const sRate  = parseFloat(document.getElementById('adm-srate').value) || null;
    const pRate  = parseFloat(document.getElementById('adm-prate').value) || null;
    const wRate  = parseFloat(document.getElementById('adm-wholesale').value) || null;
    const price  = sRate || 100;
    const cat    = document.getElementById('adm-cat').value;
    const emoji  = document.getElementById('adm-emoji').value.trim()||'🌾';
    const tags   = document.getElementById('adm-tags').value.trim();
    // Collect nutrient values
    const nutrients = {};
    const nutP = parseFloat(document.getElementById('adm-nut-protein').value);
    const nutAP = parseFloat(document.getElementById('adm-nut-abs-protein').value);
    const nutF = parseFloat(document.getElementById('adm-nut-fat').value);
    const nutC = parseFloat(document.getElementById('adm-nut-carbs').value);
    const nutFi = parseFloat(document.getElementById('adm-nut-fiber').value);
    const nutM = parseFloat(document.getElementById('adm-nut-moisture').value);
    const nutA = parseFloat(document.getElementById('adm-nut-ash').value);
    if (!isNaN(nutP)) nutrients.protein = nutP;
    if (!isNaN(nutAP)) nutrients.absorbable_protein = nutAP;
    if (!isNaN(nutF)) nutrients.fat = nutF;
    if (!isNaN(nutC)) nutrients.carbohydrates = nutC;
    if (!isNaN(nutFi)) nutrients.fiber = nutFi;
    if (!isNaN(nutM)) nutrients.moisture = nutM;
    if (!isNaN(nutA)) nutrients.ash = nutA;
    if (!nameEn) { showToast('⚠️ English name required'); return; }
    if (!sRate && sRate !== 0) { showToast('⚠️ S.Rate (Selling rate) is required'); document.getElementById('adm-srate').focus(); return; }
    if (!pRate && pRate !== 0) { showToast('⚠️ P.Rate (Purchase rate) is required'); document.getElementById('adm-prate').focus(); return; }
    if (!wRate && wRate !== 0) { showToast('⚠️ Wholesale rate is required'); document.getElementById('adm-wholesale').focus(); return; }
    try {
        const r = await fetch(`${API}/admin/products`, {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({name_english:nameEn, name_tamil:nameTa, name_en:nameEn, name_ta:nameTa,
                                  price_per_kg:price, category:cat, emoji, search_tags:tags,
                                  s_rate:sRate, p_rate:pRate, wholesale_rate:wRate, nutrients:nutrients,
                                  serial_no: (serialNo !== null && !isNaN(serialNo)) ? serialNo : null})
        });
        const d = await r.json();
        if (d.success) {
            // Upload image if selected
            const fi = document.getElementById('adm-add-image');
            if (fi && fi.files[0]) {
                showToast('⏳ Uploading image…');
                const originalFilename = fi.files[0].name;
                await new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onload = async e => {
                        try {
                            await fetch(`${API}/admin/products/${d.id}/image`, {
                                method:'POST', headers:{'Content-Type':'application/json'},
                                body: JSON.stringify({image_data: e.target.result, original_filename: originalFilename})
                            });
                            resolve();
                        } catch (err) { reject(err); }
                    };
                    reader.onerror = reject;
                    reader.readAsDataURL(fi.files[0]);
                });
            }
            showToast(`✅ Product added`);
            document.getElementById('adm-name-en').value   = '';
            const _snoEl = document.getElementById('adm-serial-no');
            if (_snoEl) _snoEl.value = '';
            document.getElementById('adm-name-ta').value   = '';
            document.getElementById('adm-srate').value     = '';
            document.getElementById('adm-prate').value     = '';
            document.getElementById('adm-wholesale').value = '';
            document.getElementById('adm-tags').value      = '';
            document.getElementById('adm-emoji').value     = '🌾';
            document.getElementById('adm-nut-protein').value = '';            document.getElementById('adm-nut-abs-protein').value = '';            document.getElementById('adm-nut-fat').value = '';            document.getElementById('adm-nut-carbs').value = '';            document.getElementById('adm-nut-fiber').value = '';            document.getElementById('adm-nut-moisture').value = '';            document.getElementById('adm-nut-ash').value = '';
            // Clear image upload
            const addImgInput = document.getElementById('adm-add-image');
            if (addImgInput) addImgInput.value = '';
            const addImgPreview = document.getElementById('adm-add-img-preview');
            if (addImgPreview) { addImgPreview.src = ''; addImgPreview.style.display = 'none'; }
            _taAutoFilled = false;
            await fetchProducts();
        }
    } catch { showToast('❌ Add failed'); }
}

// ════════════════════════════════════════════════════
// KEYBOARD NAVIGATION  –  Smooth arrow-key browsing + field nav  –  Smooth arrow-key browsing + field nav
// ════════════════════════════════════════════════════
let _kbIndex = -1;   // currently highlighted product card index (-1 = none)

function _getVisibleCards() {
    return Array.from(document.querySelectorAll('#products-grid .pcard'));
}

function _getColumnsPerRow() {
    const cards = _getVisibleCards();
    if (!cards.length) return 1;
    const firstTop = cards[0].getBoundingClientRect().top;
    let cols = 0;
    for (const c of cards) {
        if (Math.abs(c.getBoundingClientRect().top - firstTop) < 5) cols++;
        else break;
    }
    return Math.max(1, cols);
}

function _highlightCard(newIndex) {
    const cards = _getVisibleCards();
    if (!cards.length) return;
    // Remove old highlight
    if (_kbIndex >= 0 && _kbIndex < cards.length)
        cards[_kbIndex].classList.remove('kb-focus');
    // Clamp new index
    _kbIndex = Math.max(0, Math.min(newIndex, cards.length - 1));
    const card = cards[_kbIndex];
    card.classList.add('kb-focus');
    // Smooth scroll – keep selected card visible without jumping the whole grid
    card.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
}

function _clearHighlight() {
    _getVisibleCards().forEach(c => c.classList.remove('kb-focus'));
    _kbIndex = -1;
}

// Weight modal field order for LEFT/RIGHT navigation
const _WT_FIELDS = ['wt-input', 'wt-unit', 'wt-temp-price'];

// ════════════════════════════════════════════════════
// MODAL STACK MANAGER  (production-grade, pywebview-safe)
// ════════════════════════════════════════════════════
// All modals register/unregister here. ESC always closes the top-most active modal.
// Works regardless of focus state — critical for pywebview where focus is unreliable.
const ModalManager = (() => {
    // Stack entries: { id, closeFn, priority }
    const _stack = [];

    function register(id, closeFn, priority = 0) {
        // Avoid duplicate registration
        if (_stack.find(m => m.id === id)) return;
        _stack.push({ id, closeFn, priority });
        // Higher priority = closer to top
        _stack.sort((a, b) => b.priority - a.priority);
    }

    function unregister(id) {
        const idx = _stack.findIndex(m => m.id === id);
        if (idx !== -1) _stack.splice(idx, 1);
    }

    function closeTop() {
        // Find the top-most actually-open modal
        for (let i = 0; i < _stack.length; i++) {
            const m = _stack[i];
            const el = document.getElementById(m.id);
            if (el && el.classList.contains('open')) {
                m.closeFn();
                return true;
            }
        }
        return false; // No modal was open
    }

    function anyOpen() {
        return _stack.some(m => {
            const el = document.getElementById(m.id);
            return el && el.classList.contains('open');
        });
    }

    function openCount() {
        return _stack.filter(m => {
            const el = document.getElementById(m.id);
            return el && el.classList.contains('open');
        }).length;
    }

    return { register, unregister, closeTop, anyOpen, openCount };
})();

// ════════════════════════════════════════════════════
// GLOBAL ESC KEY HANDLER  (pywebview-safe)
// ════════════════════════════════════════════════════
// Uses capture phase (true) so it fires regardless of which element has focus.
// This is the ONLY place ESC is handled globally — no per-modal ESC listeners needed.
(function _installGlobalEscHandler() {
    function _onKeydown(e) {
        if (e.key !== 'Escape' && e.key !== 'Esc') return;

        // Always prevent default to avoid browser/webview back navigation
        e.preventDefault();
        e.stopPropagation();

        // 1. Close confirmation dialog if open (highest priority)
        const kh_handled = _tryCloseConfirmDialog();
        if (kh_handled) return;

        // 2. Close top-most modal via ModalManager
        const closedModal = ModalManager.closeTop();
        if (closedModal) return;

        // 3. No modal open — clear search and reset focus
        const s = document.getElementById('productSearch');
        if (s && s.value) {
            s.value = '';
            if (typeof renderProducts === 'function') renderProducts();
        }
        if (typeof resetFocus === 'function') resetFocus();
        if (typeof _clearHighlight === 'function') _clearHighlight();
    }

    // Check for pw-change-modal (uses display:none/flex instead of .open class)
    function _tryCloseConfirmDialog() {
        const pw = document.getElementById('pw-change-modal');
        if (pw && pw.style.display === 'flex') {
            pw.style.display = 'none';
            return true;
        }
        const cp = document.getElementById('cp-modal');
        if (cp && cp.classList.contains('open')) {
            if (typeof closeCollectPayment === 'function') closeCollectPayment();
            return true;
        }
        return false;
    }

    // Register in capture phase — fires before any element's keydown handler
    document.addEventListener('keydown', _onKeydown, true);

    // pywebview safety: also bind to window in case document loses focus
    window.addEventListener('keydown', function(e) {
        if (e.key !== 'Escape' && e.key !== 'Esc') return;
        if (e.target === document.body || e.target === document.documentElement || !document.activeElement || document.activeElement === document.body) {
            _onKeydown(e);
        }
    }, true);
})();

// ── Register all modals with ModalManager ─────────────────────────────────
// Called after DOM is ready (see DOMContentLoaded below via _registerModals)
function _registerAllModals() {
    ModalManager.register('wt-modal',          () => { if(typeof closeWeightModal==='function') closeWeightModal(); },   50);
    ModalManager.register('pkg-modal',         () => { if(typeof closePackageModal==='function') closePackageModal(); }, 50);
    ModalManager.register('edit-modal',        () => { if(typeof closeEditModal==='function') closeEditModal(); },       50);
    ModalManager.register('bill-detail-modal', () => { if(typeof closeBillDetail==='function') closeBillDetail(); },     50);
    ModalManager.register('cust-hist-modal',   () => { if(typeof closeCustHistory==='function') closeCustHistory(); },   50);
    ModalManager.register('price-mode-modal',  () => { if(typeof cancelPriceModeChange==='function') cancelPriceModeChange(); }, 40);
}

// ════════════════════════════════════════════════════
// CENTRALISED KEYBOARD HANDLER  (v-patch)
// ════════════════════════════════════════════════════
document.addEventListener('keydown', e => {
    // ESC is fully handled by the global ESC handler above — skip here
    if (e.key === 'Escape' || e.key === 'Esc') return;

    const key = _eventToKey(e);
    const handlers = _getShortcutHandlers();

    const anyModalOpen = ModalManager.anyOpen();

    // ── Configurable shortcut dispatch ────────────────────────────────────────
    // AUDIT FIX: while the cashier is typing in a text field, never steal plain
    // letters/digits (e.g. repeatBill='r' blocked typing "ragi") or the standard
    // editing keys (Ctrl+C/V/X/A/Z/Y were mapped to Credit/Admin in the DB).
    const _ae = document.activeElement;
    const _typing = _ae && (_ae.tagName === 'INPUT' || _ae.tagName === 'TEXTAREA' || _ae.isContentEditable);
    const _EDIT_KEYS = ['ctrl+c', 'ctrl+v', 'ctrl+x', 'ctrl+a', 'ctrl+z', 'ctrl+y'];
    const _plainKey = !e.ctrlKey && !e.metaKey && !e.altKey && e.key.length === 1;
    for (const [action, shortcutKey] of Object.entries(_shortcuts)) {
        if (action === 'exit') continue;   // handled above
        if (key === shortcutKey) {
            if (_typing && (_plainKey || _EDIT_KEYS.includes(key))) break;   // let the field have it
            const allowWhileModal = ['save', 'printBill'];
            if (anyModalOpen && !allowWhileModal.includes(action)) return;
            e.preventDefault();
            handlers[action]?.();
            return;
        }
    }

    // ── Legacy non-configurable shortcuts (preserved intact) ──────────────────
    // Ctrl+K → focus search
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        const s = document.getElementById('productSearch');
        if (s) { s.focus(); s.select(); }
        return;
    }
    // Alt+S → focus search
    if (e.altKey && e.key.toLowerCase() === 's') {
        e.preventDefault();
        const s = document.getElementById('productSearch');
        if (s) { s.focus(); s.select(); }
        return;
    }

    const wtOpen   = document.getElementById('wt-modal').classList.contains('open');
    const pkgOpen  = document.getElementById('pkg-modal').classList.contains('open');
    const editOpen = document.getElementById('edit-modal').classList.contains('open');

    // ── Package modal navigation ───────────────────────────────────────────────
    if (pkgOpen) {
        if (e.key === 'Enter') {
            const focused = document.activeElement;
            if (!focused || !focused.classList.contains('pkg-item-qty-input')) {
                e.preventDefault();
                confirmAddPackage();
            }
        }
        return;
    }

    // ── Weight modal navigation ────────────────────────────────────────────────
    if (wtOpen) {
        if (e.key === 'Enter') { e.preventDefault(); confirmAdd(); return; }
        if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            const focused = document.activeElement?.id;
            const idx     = _WT_FIELDS.indexOf(focused);
            if (idx >= 0) {
                e.preventDefault();
                const next = e.key === 'ArrowRight'
                    ? Math.min(idx + 1, _WT_FIELDS.length - 1)
                    : Math.max(idx - 1, 0);
                const el = document.getElementById(_WT_FIELDS[next]);
                if (el) { el.focus(); el.select && el.select(); }
            }
        }
        return;
    }
    if (editOpen) return;

    // ── Input / Textarea guard ─────────────────────────────────────────────────
    const tag = document.activeElement?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
        if (document.activeElement?.id === 'productSearch') {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                document.activeElement.blur();
                _highlightCard(0);
                return;
            }
            if (e.key === 'Enter') {
                e.preventDefault();
                // Fix: use correct selectors (.pcard + .kb-focus) that match the actual DOM
                const highlighted = document.querySelector('#products-grid .pcard.kb-focus');
                const first       = highlighted || document.querySelector('#products-grid .pcard');
                if (first) {
                    const cardId = first.id.replace('pcard-', '');
                    if (packages.find(p => p.id === cardId)) openPackageModal(cardId);
                    else openWeightModal(cardId);
                }
                return;
            }
            // Printable char typed in search: auto-highlight first result after filter renders
            if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
                setTimeout(() => { if (_kbIndex < 0) _highlightCard(0); }, 0);
            }
        }
        if (e.altKey && e.key.toLowerCase() === 's') {
            e.preventDefault();
            document.getElementById('productSearch')?.focus();
            return;
        }
        return;
    }

    // ── Z key → New Bill (only fires when no input focused) ───────────────────
    if (e.key.toLowerCase() === 'z' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        e.stopPropagation();
        const anyModalOpen2 = document.getElementById('wt-modal')?.classList.contains('open')
            || document.getElementById('pkg-modal')?.classList.contains('open');
        if (!anyModalOpen2) { _getShortcutHandlers().newBill?.(); }
        return;
    }

    // ── Product grid arrow / Enter navigation ─────────────────────────────────
    const billingVisible = document.getElementById('section-billing').style.display !== 'none';
    if (!billingVisible) return;

    const cards = _getVisibleCards();
    if (!cards.length) return;
    const cols = _getColumnsPerRow();

    if (['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)) {
        e.preventDefault();
        e.stopPropagation();
    }

    switch (e.key) {
        case 'ArrowRight': _highlightCard(_kbIndex < 0 ? 0 : _kbIndex + 1); break;
        case 'ArrowLeft':  _highlightCard(_kbIndex <= 0 ? 0 : _kbIndex - 1); break;
        case 'ArrowDown':  _highlightCard(_kbIndex < 0 ? 0 : _kbIndex + cols); break;
        case 'ArrowUp':    _highlightCard(_kbIndex < 0 ? 0 : Math.max(0, _kbIndex - cols)); break;
        case 'Enter':
            e.preventDefault();
            if (_kbIndex >= 0 && _kbIndex < cards.length) {
                const cardId = cards[_kbIndex].id.replace('pcard-', '');
                if (packages.find(p => p.id === cardId)) openPackageModal(cardId);
                else openWeightModal(cardId);
            }
            break;
    }
});

// ════════════════════════════════════════════════════
// ADMIN – EXPORT / BACKUP
// ════════════════════════════════════════════════════
async function adminDownloadDB() {
    showToast('💾 Saving database backup to Downloads…');
    try {
        const r = await fetch(`${API}/admin/download-db?local=1`);
        const d = await r.json();
        if (d.success) showToast('✅ Saved: ' + d.path);
        else showToast('❌ Failed to save backup');
    } catch { showToast('⚠️ Server offline'); }
}

async function adminExportBills() {
    showToast('📊 Saving bills Excel to Downloads…');
    try {
        const r = await fetch(`${API}/admin/export/bills?local=1`);
        const d = await r.json();
        if (d.success) showToast('✅ Saved: ' + d.path);
        else showToast('❌ Failed to save bills');
    } catch { showToast('⚠️ Server offline'); }
}

async function adminExportProducts() {
    showToast('📊 Saving products Excel to Downloads…');
    try {
        const r = await fetch(`${API}/admin/export/products?local=1`);
        const d = await r.json();
        if (d.success) showToast('✅ Saved: ' + d.path);
        else showToast('❌ Failed to save products');
    } catch { showToast('⚠️ Server offline'); }
}

// ════════════════════════════════════════════════════
// WHATSAPP BILL
// ════════════════════════════════════════════════════
async function sendBillToWhatsApp(bill) {
    const typedPhone = document.getElementById('customer-phone')?.value.trim() || '';
    const hasLiveCart = cart.length > 0;
    let waBill = (!hasLiveCart && (bill || window.currentBill)) ? (bill || window.currentBill) : null;
    const useLiveCart = hasLiveCart || !waBill;
    if (useLiveCart && !cart.length) { showToast('Cart is empty'); return; }

    const prePhone = (waBill ? (waBill.customer_phone || '') : typedPhone).replace(/\D/g, '');
    if (!prePhone) { showToast('Enter customer phone number first'); return; }

    if (useLiveCart) {
        waBill = await _saveBillCore({ silent: true });
        if (!waBill) {
            showToast('Could not prepare WhatsApp bill');
            return;
        }
    }

    const rawPhone = waBill ? (waBill.customer_phone || '') : document.getElementById('customer-phone').value.trim();
    const phone = rawPhone.replace(/\D/g, '');
    if (!phone) { showToast('Enter customer phone number first'); return; }

    const now = new Date();
    const dateStr = waBill
        ? ((waBill.timestamp || waBill.created_at || '').split(' ')[0])
        : now.toLocaleDateString('en-IN', {day:'2-digit', month:'2-digit', year:'numeric'});
    const custName = waBill ? waBill.customer_name : document.getElementById('customer-name').value.trim();

    let items;
    if (waBill) {
        items = Array.isArray(waBill.items) ? waBill.items
              : (typeof waBill.items === 'string' ? JSON.parse(waBill.items) : []);
    } else {
        items = cart.map(({product, weight_g, price, temp_price_per_kg}) => ({
            product_id: product.id,
            name: product.displayName,
            weight_g,
            total_price: price,
            price_per_kg: temp_price_per_kg !== null ? temp_price_per_kg : product.price_per_kg,
            nutrition: product.nutrition || null,
        }));
    }

    const total = waBill
        ? parseFloat(waBill.total_amount != null ? waBill.total_amount : waBill.total)
        : cart.reduce((s, i) => s + i.price, 0);

    // Build plain-text message (fast, no image/PDF)
    let msg = '🧾 DHANA DHANYA KADAI\n';
    if (custName) msg += 'Customer: ' + custName + '\n';
    msg += 'Date: ' + dateStr + '\n';
    msg += '-----------------------\n';
    msg += 'Items:\n';
    items.forEach(item => {
        const wt = item.weight_g >= 1000
            ? (item.weight_g / 1000).toFixed(2) + 'kg'
            : item.weight_g + 'g';
        msg += '• ' + item.name + ' (' + wt + ') → ₹' + parseFloat(item.total_price).toFixed(2) + '\n';
    });
    msg += '-----------------------\n';
    msg += 'Total: ₹' + total.toFixed(2) + '\n\n';

    // Nutrition summary — same source as print bill (window.currentBill nutr)
    const _wcb = waBill || window.currentBill || {};
    let _nutr = _wcb.nutr || {};
    if (!_nutr.protein && !_nutr.fat && !_nutr.carbs) {
        const _t = (_wcb.nutrition_display && _wcb.nutrition_display.totals) || {};
        if (_t.protein || _t.fat || _t.carbohydrates) {
            _nutr = {
                protein:    _t.protein           || 0,
                absorbable: _t.absorbable_protein || 0,
                fat:        _t.fat               || 0,
                carbs:      _t.carbohydrates     || 0,
                fiber:      _t.fiber             || 0,
                ash:        _t.ash               || 0,
                moisture:   _t.moisture          || 0,
                has_oil:    !!(_wcb.nutrition_display && _wcb.nutrition_display.has_oil),
                has_normal: !!(_wcb.nutrition_display && _wcb.nutrition_display.has_normal),
            };
        }
    }
    const _MIN = 0.5;
    const _r1  = x => Math.round(parseFloat(x || 0) * 10) / 10;
    const _wp = _r1(_nutr.protein), _wf = _r1(_nutr.fat), _wc = _r1(_nutr.carbs);
    const _wfb = _r1(_nutr.fiber),  _wm = _r1(_nutr.moisture), _wa = _r1(_nutr.ash);
    const _wab = _r1(_nutr.absorbable);
    const _wHasOil = !!_nutr.has_oil || ((_wcb.package_mode || _wcb._packageMode || '') === 'oil');
    if (_wp >= _MIN || _wf >= _MIN || _wc >= _MIN) {
        msg += '🔬 NUTRITION SUMMARY 🔬\n';
        msg += 'per 100g · values in %\n';
        msg += '-----------------------\n';
        if (_wp >= _MIN) {
            msg += 'Protein: ' + _wp.toFixed(1) + '%\n';
            if (!_wHasOil && _wab >= _MIN) msg += '  ↳ Absorbable: ' + _wab.toFixed(1) + '%\n';
        }
        if (_wf >= _MIN)  msg += 'Fat: '           + _wf.toFixed(1) + '%\n';
        if (_wc >= _MIN)  msg += 'Carbohydrates: ' + _wc.toFixed(1) + '%\n';
        if (_wfb > _MIN)  msg += 'Fiber: '         + _wfb.toFixed(1) + '%\n';
        if (_wHasOil) {
            if (_wm > _MIN) msg += 'Moisture: ' + _wm.toFixed(1) + '%\n';
            if (_wa >= _MIN) msg += 'Ash: '     + _wa.toFixed(1) + '%\n';
        } else {
            if (_wa >= _MIN) msg += 'Ash: '     + _wa.toFixed(1) + '%\n';
        }
        msg += 'Source: USDA / IFCT 2017\n\n';
    }

    msg += 'Thank you 🙏';

    window.open('https://wa.me/' + phone + '?text=' + encodeURIComponent(msg), '_blank');
}

// ════════════════════════════════════════════════════
// LOYALTY SETTINGS (Admin)
// ════════════════════════════════════════════════════
async function loadLoyaltySettings() {
    try {
        const r = await fetch(`${API}/api/loyalty-settings`);
        const d = await r.json();
        const s = d.settings || {};
        const el = document.getElementById('loyalty-settings-form');
        if (!el) return;
        document.getElementById('ls-enabled').checked       = !!s.loyalty_enabled;
        document.getElementById('ls-min-purchase').value    = s.min_purchase_for_star || 500;
        document.getElementById('ls-stars-reward').value    = s.stars_for_reward || 10;
        document.getElementById('ls-discount-amt').value    = s.reward_discount || 100;
    } catch { showToast('⚠️ Could not load loyalty settings'); }
}

async function saveLoyaltySettings() {
    const payload = {
        loyalty_enabled:       document.getElementById('ls-enabled').checked,
        min_purchase_for_star: parseFloat(document.getElementById('ls-min-purchase').value)||500,
        stars_for_reward:      parseInt(document.getElementById('ls-stars-reward').value)||10,
        reward_discount:       parseFloat(document.getElementById('ls-discount-amt').value)||100,
    };
    try {
        const r = await fetch(`${API}/api/loyalty-settings`, {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(payload)
        });
        const d = await r.json();
        if (d.success) {
            // Sync in-memory cache immediately so receipt + loyalty panel reflect new setting right away
            _loyaltySettings = {
                enabled:        !!payload.loyalty_enabled,
                min_purchase:   payload.min_purchase_for_star,
                stars_required: payload.stars_for_reward,
                discount:       payload.reward_discount
            };
            showToast('✅ Loyalty settings saved!');
        } else {
            showToast('❌ Failed to save settings');
        }
    } catch { showToast('⚠️ Server offline'); }
}

// ════════════════════════════════════════════════════
// TOAST
// ════════════════════════════════════════════════════
let _toastTimer;
function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent=msg; t.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(()=>t.classList.remove('show'), 2600);
}

// ═══════════════════════════════════════════════════════════════
// PACKAGE ADMIN — Full CRUD with ingredients + nutrients
// ═══════════════════════════════════════════════════════════════

let _adminPackages = [];   // cache
let _pkgIngredients = [];  // working copy for form
let _lastAdminTab = 'products'; // remembers active admin tab

const NUTR_KEYS = ['protein','fat','carbohydrates','fiber','moisture','ash','absorbable_protein'];
const NUTR_LABELS = {
    protein:'Protein (g)', fat:'Fat (g)', carbohydrates:'Carbohydrates (g)',
    fiber:'Fiber (g)', moisture:'Moisture (g)', ash:'Ash (g)', absorbable_protein:'Absorbable Protein (g)'
};

// ── Load & render list ─────────────────────────────────────────
async function loadAdminPackages() {
    const listEl = document.getElementById('pkg-admin-list');
    listEl.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:12px 0;">⏳ Loading…</div>';
    try {
        const r = await fetch(`${API}/packages`);
        const d = await r.json();
        _adminPackages = d.packages || [];
        renderAdminPackageList();
    } catch(e) {
        listEl.innerHTML = '<div style="color:var(--red);font-size:13px;">⚠️ Failed to load packages.</div>';
    }
}

function renderAdminPackageList() {
    const el = document.getElementById('pkg-admin-list');
    if (!_adminPackages.length) {
        el.innerHTML = '<p style="color:var(--muted);font-size:13px;padding:12px 0;">No packages yet. Click ➕ New Package to create one.</p>';
        return;
    }
    el.innerHTML = _adminPackages.map(pkg => {
        const totalKg = (pkg.items||[]).reduce((s,it) => s + (parseFloat(it.ratio_kg)||0), 0);
        const ingredientNames = (pkg.items||[]).map(it => it.name_english).join(', ');
        return `
        <div onclick="openPackageForm('${pkg.id}')"
          style="display:flex;justify-content:space-between;align-items:center;padding:12px 14px;
                 border:1.5px solid var(--border);border-radius:var(--radius-sm);margin-bottom:8px;
                 cursor:pointer;transition:all .15s;background:#fff;"
          onmouseover="this.style.borderColor='var(--blue)';this.style.background='var(--blue-xlt)'"
          onmouseout="this.style.borderColor='var(--border)';this.style.background='#fff'">
          <div>
            <div style="font-size:14px;font-weight:700;">${pkg.emoji||'📦'} ${pkg.name_english}
              ${pkg.name_tamil ? `<span style="color:var(--muted);font-size:12px;font-weight:500;margin-left:6px;">${pkg.name_tamil}</span>` : ''}
            </div>
            <div style="font-size:11.5px;color:var(--muted);margin-top:3px;">
              ${(pkg.items||[]).length} ingredients · ${totalKg} kg total mix
              <span style="margin-left:8px;color:var(--text-2);">${ingredientNames.substring(0,60)}${ingredientNames.length>60?'…':''}</span>
            </div>
          </div>
          <div style="text-align:right;flex-shrink:0;margin-left:12px;">
            <div style="font-size:16px;font-weight:800;color:var(--blue);"><span class="currency">₹</span><span class="value">${pkg.price_per_kg}</span><small style="font-size:10px;font-weight:500;color:var(--muted);">/kg</small></div>
            <div style="font-size:10px;color:var(--muted);margin-top:2px;">click to edit</div>
          </div>
        </div>`;
    }).join('');
}

// ── Open form (null = new, id = edit) ─────────────────────────
function openPackageForm(pkgId) {
    const formCard = document.getElementById('pkg-admin-form-card');
    const titleEl  = document.getElementById('pkg-form-title');
    const deleteBtn = document.getElementById('pkg-delete-btn');

    document.getElementById('pkg-nutr-section').style.display = 'none';
    document.getElementById('pkg-nutr-toggle-btn').textContent = '🧬 Show Nutrient Values ▼';

    if (!pkgId) {
        // New package
        document.getElementById('pkg-edit-id').value = '';
        document.getElementById('pkg-name-en').value = '';
        document.getElementById('pkg-name-ta').value = '';
        document.getElementById('pkg-price').value = '';
        document.getElementById('pkg-price').readOnly = true;
        document.getElementById('pkg-price').style.background = 'var(--bg)';
        document.getElementById('pkg-price').style.color = 'var(--muted)';
        if (document.getElementById('pkg-manual-price-toggle')) {
            document.getElementById('pkg-manual-price-toggle').checked = false;
        }
        document.getElementById('pkg-emoji').value = '📦';
        if (document.getElementById('pkg-product-mode')) document.getElementById('pkg-product-mode').value = 'normal';
        _resetPkgImageUI();
        _pkgIngredients = [];
        _pkgOverallNutr = { protein:0, fat:0, carbohydrates:0, fiber:0, moisture:0, ash:0, absorbable_protein:0 };
        _nutrMode = 'overall';
        titleEl.textContent = '➕ New Package';
        deleteBtn.style.display = 'none';
    } else {
        // Edit existing
        const pkg = _adminPackages.find(p => p.id === pkgId);
        if (!pkg) return;
        document.getElementById('pkg-edit-id').value = pkg.id;
        document.getElementById('pkg-name-en').value = pkg.name_english || '';
        document.getElementById('pkg-name-ta').value = pkg.name_tamil || '';
        document.getElementById('pkg-price').value = pkg.price_per_kg || '';
        if (document.getElementById('pkg-manual-price-toggle')) {
            document.getElementById('pkg-manual-price-toggle').checked = true;
            toggleManualPrice(true);
        }
        document.getElementById('pkg-emoji').value = pkg.emoji || '📦';
        if (document.getElementById('pkg-product-mode')) document.getElementById('pkg-product-mode').value = pkg.product_mode || 'normal';
        _resetPkgImageUI();
        _loadPkgImagePreview(pkg.image || '');
        _pkgIngredients = (pkg.items || []).map(it => ({...it}));
        // Load overall nutrition override if present
        const ov = pkg.nutrition_override;
        if (ov && (ov.protein || ov.fat || ov.carbohydrates)) {
            _pkgOverallNutr = { protein:0, fat:0, carbohydrates:0, fiber:0, moisture:0, ash:0, absorbable_protein:0, ...ov };
            _nutrMode = 'overall';
        } else {
            _pkgOverallNutr = { protein:0, fat:0, carbohydrates:0, fiber:0, moisture:0, ash:0, absorbable_protein:0 };
            _nutrMode = 'per-ing';
        }
        titleEl.textContent = `✏️ Edit: ${pkg.name_english}`;
        deleteBtn.style.display = '';
    }

    renderIngredientRows();
    renderNutrientRows();
    formCard.style.display = '';
    // Clear any leftover receipt HTML so it doesn't show below the form
    const pa = document.getElementById('print-area');
    if (pa) pa.innerHTML = '';
    // Scroll to form after DOM settles
    setTimeout(() => {
        const form = document.getElementById('pkg-admin-form-card');
        if (form) form.scrollIntoView({ behavior: 'smooth', block: 'start' });
        else window.scrollTo({ top: 0, behavior: 'smooth' });
    }, 80);
}

function closePackageForm() {
    document.getElementById('pkg-admin-form-card').style.display = 'none';
}

// ── Tamil global toggle for all pkg Tamil fields ───────────────
// Track last focused Tamil input in package form (for virtual keyboard)
let _lastPkgTamilId = 'pkg-name-ta';
function _onPkgTamilFocus(id) { _lastPkgTamilId = id; }
function togglePkgTamilKeyboard() {
    tamilKeyboard.toggle(_lastPkgTamilId || 'pkg-name-ta');
}

function togglePkgTamilTranslit() {
    const wasOn = tamilTranslit.isEnabled('pkg-name-ta');
    tamilTranslit.toggle('pkg-name-ta');
    const nowOn = !wasOn;
    // Sync all visible ingredient Tamil fields
    _pkgIngredients.forEach((_, i) => {
        const taId = `ing-ta-${i}`;
        if (document.getElementById(taId)) {
            if (nowOn) tamilTranslit.enable(taId);
            else tamilTranslit.disable(taId);
        }
    });
    // Update button label
    const btn = document.getElementById('pkg-name-ta-translit-toggle');
    if (btn) {
        btn.textContent = nowOn ? '⌨️ தமிழ் ON' : '⌨️ ENG';
        btn.style.background = nowOn ? '#e8f5e9' : '';
        btn.style.color = nowOn ? '#2e7d32' : '';
    }
}

// ── Ingredient rows ────────────────────────────────────────────
function addIngredientRow() {
    _pkgIngredients.push({
        name_english: '', name_tamil: '', ratio_kg: 1, price_per_kg: 0,
        nutrients_per_100g: { protein:0, fat:0, carbohydrates:0, fiber:0, moisture:0, ash:0, absorbable_protein:0 }
    });
    renderIngredientRows();
    renderNutrientRows();
}

function removeIngredientRow(idx) {
    _pkgIngredients.splice(idx, 1);
    renderIngredientRows();
    renderNutrientRows();
    _recalcPkgPrice();
}

function renderIngredientRows() {
    const el = document.getElementById('pkg-ingredients-list');
    if (!_pkgIngredients.length) {
        el.innerHTML = '<p style="color:var(--muted);font-size:12px;padding:8px 0;">No ingredients yet. Use buttons above to add.</p>';
        _recalcPkgPrice();
        return;
    }
    el.innerHTML = _pkgIngredients.map((ing, i) => {
        const taId = `ing-ta-${i}`;
        const fromProd = ing._from_product ? ' style="background:var(--bg);" readonly' : '';
        return `
      <div class="ing-row" style="display:grid;grid-template-columns:2fr 2fr 0.7fr 0.7fr auto;gap:10px;align-items:center;padding:4px 0;">
        <input type="text" id="ing-en-${i}" value="${(ing.name_english||'').replace(/"/g,'&quot;')}" placeholder="English name"
          oninput="updateIngField(${i},'name_english',this.value)"${fromProd}
          style="padding:7px 10px;border:1.5px solid var(--border);border-radius:6px;font-size:12px;outline:none;font-family:var(--font);width:100%;box-sizing:border-box;" />
        <div style="display:flex;gap:4px;align-items:center;">
          <input type="text" id="${taId}" value="${(ing.name_tamil||'').replace(/"/g,'&quot;')}" placeholder="Tamil name"
            lang="ta" autocomplete="off"
            oninput="updateIngField(${i},'name_tamil',this.value)"
            onfocus="_onPkgTamilFocus('${taId}')"
            style="padding:7px 10px;border:1.5px solid var(--border);border-radius:6px;font-size:12px;outline:none;font-family:var(--font);flex:1;min-width:0;" />
          <button id="${taId}-kbd-toggle" type="button"
            onclick="tamilKeyboard.toggle('${taId}')"
            title="Open Tamil Keyboard for this row"
            style="padding:4px 6px;border:1.5px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer;background:var(--bg);color:var(--fg);white-space:nowrap;font-family:var(--font);flex-shrink:0;line-height:1;">⌨️</button>
        </div>
        <input type="number" value="${ing.ratio_kg||1}" min="0" step="0.5" placeholder="kg"
          oninput="updateIngField(${i},'ratio_kg',parseFloat(this.value)||0)"
          style="padding:7px 8px;border:1.5px solid var(--border);border-radius:6px;font-size:12px;outline:none;font-family:var(--font);text-align:center;width:100%;box-sizing:border-box;" />
        <input type="number" value="${ing.price_per_kg||0}" min="0" placeholder="₹"
          oninput="updateIngField(${i},'price_per_kg',parseFloat(this.value)||0)"
          style="padding:7px 8px;border:1.5px solid var(--border);border-radius:6px;font-size:12px;outline:none;font-family:var(--font);text-align:center;width:100%;box-sizing:border-box;" />
        <button onclick="removeIngredientRow(${i})"
          style="width:28px;height:28px;border:1.5px solid #FECACA;border-radius:5px;background:var(--red-lt);color:var(--red);cursor:pointer;font-size:13px;font-weight:700;font-family:var(--font);display:flex;align-items:center;justify-content:center;flex-shrink:0;">✕</button>
      </div>`;
    }).join('');

    // Re-apply Tamil translit to all ingredient Tamil fields if globally enabled
    _bindIngTamilFields();
    _recalcPkgPrice();
}

function _bindIngTamilFields() {
    // Bind transliteration and focus-tracking to each ingredient Tamil input
    const pkgTranslitOn = tamilTranslit.isEnabled('pkg-name-ta');
    _pkgIngredients.forEach((ing, i) => {
        const taId = `ing-ta-${i}`;
        const el = document.getElementById(taId);
        if (!el) return;

        // Register with translit engine (attaches event listeners once via state.initialized)
        // Then enable/disable based on current global translit state
        tamilTranslit.enable(taId);
        if (!pkgTranslitOn) tamilTranslit.disable(taId);

        // Track focus for virtual keyboard: when this field is focused, keyboard targets it
        if (!el._pkgTamilBound) {
            el._pkgTamilBound = true;
            el.addEventListener('focus', () => { _onPkgTamilFocus(taId); });
            // oninput already syncs via the inline oninput attribute in the template
        }
    });
}

function _recalcPkgPrice() {
    const manualOn = document.getElementById('pkg-manual-price-toggle')?.checked;
    const priceField = document.getElementById('pkg-price');
    const summary = document.getElementById('pkg-price-summary');
    const costDisp = document.getElementById('pkg-cost-display');
    const sellDisp = document.getElementById('pkg-sell-display');
    const profitDisp = document.getElementById('pkg-profit-display');

    if (!priceField) return;

    const totalKg = _pkgIngredients.reduce((s, ing) => s + (parseFloat(ing.ratio_kg)||0), 0);
    const totalCost = _pkgIngredients.reduce((s, ing) => s + (parseFloat(ing.ratio_kg)||0) * (parseFloat(ing.price_per_kg)||0), 0);
    const costPerKg = totalKg > 0 ? totalCost / totalKg : 0;

    if (!manualOn) {
        priceField.value = costPerKg > 0 ? costPerKg.toFixed(2) : '';
    }

    const sellPrice = parseFloat(priceField.value) || 0;
    const profit = sellPrice - costPerKg;

    if (_pkgIngredients.length > 0) {
        summary.style.display = '';
        costDisp.textContent = `₹${costPerKg.toFixed(2)}/kg`;
        sellDisp.textContent = `₹${sellPrice.toFixed(2)}/kg`;
        profitDisp.textContent = `₹${profit.toFixed(2)}/kg`;
        profitDisp.parentElement.style.color = profit >= 0 ? '#16a34a' : '#dc2626';
        profitDisp.style.color = profit >= 0 ? '#16a34a' : '#dc2626';
    } else {
        summary.style.display = 'none';
    }
}

function updateIngField(idx, field, val) {
    if (_pkgIngredients[idx]) {
        _pkgIngredients[idx][field] = val;
        if (field === 'name_english') renderNutrientRows();
        if (field === 'ratio_kg' || field === 'price_per_kg') _recalcPkgPrice();
    }
}

// ── Nutrient rows ──────────────────────────────────────────────
// Tracks which nutrition mode is active: 'overall' or 'per-ing'
let _nutrMode = 'overall';
// Stores the overall nutrition values entered by the user
let _pkgOverallNutr = { protein:0, fat:0, carbohydrates:0, fiber:0, moisture:0, ash:0, absorbable_protein:0 };

function toggleNutrientSection() {
    const sec = document.getElementById('pkg-nutr-section');
    const btn = document.getElementById('pkg-nutr-toggle-btn');
    const visible = sec.style.display !== 'none';
    sec.style.display = visible ? 'none' : '';
    btn.textContent = visible ? '🧬 Show Nutrient Values ▼' : '🧬 Hide Nutrient Values ▲';
    if (!visible) { setNutrMode(_nutrMode); }
}

function setNutrMode(mode) {
    _nutrMode = mode;
    const overallPanel = document.getElementById('pkg-nutr-overall-panel');
    const perIngPanel  = document.getElementById('pkg-nutr-per-ing-panel');
    const btnOverall   = document.getElementById('nutr-mode-overall');
    const btnPerIng    = document.getElementById('nutr-mode-per-ing');
    if (mode === 'overall') {
        overallPanel.style.display = '';
        perIngPanel.style.display  = 'none';
        btnOverall.style.background = '#1d4ed8';
        btnOverall.style.color      = '#fff';
        btnPerIng.style.background  = 'var(--bg)';
        btnPerIng.style.color       = 'var(--muted)';
        renderOverallNutrGrid();
    } else {
        overallPanel.style.display = 'none';
        perIngPanel.style.display  = '';
        btnOverall.style.background = 'var(--bg)';
        btnOverall.style.color      = 'var(--muted)';
        btnPerIng.style.background  = '#1d4ed8';
        btnPerIng.style.color       = '#fff';
        renderNutrientRows();
    }
}

function renderOverallNutrGrid() {
    const grid = document.getElementById('pkg-nutr-overall-grid');
    if (!grid) return;
    grid.innerHTML = NUTR_KEYS.map(k => `
        <div>
          <label style="font-size:10px;font-weight:700;color:var(--muted);display:block;margin-bottom:3px;">${NUTR_LABELS[k]}</label>
          <input type="number" min="0" max="100" step="0.1" value="${_pkgOverallNutr[k]||0}"
            oninput="validateNutrientInput(this); _pkgOverallNutr['${k}']=parseFloat(this.value)||0"
            style="width:100%;padding:6px 8px;border:1.5px solid var(--border);border-radius:5px;font-size:12px;outline:none;font-family:var(--font);" />
        </div>`).join('');
}

function renderNutrientRows() {
    const el = document.getElementById('pkg-nutr-rows-admin');
    if (!_pkgIngredients.length) {
        el.innerHTML = '<p style="color:var(--muted);font-size:12px;">Add ingredients first.</p>';
        return;
    }
    el.innerHTML = _pkgIngredients.map((ing, i) => {
        const n = ing.nutrients_per_100g || {};
        const label = ing.name_english || `Ingredient ${i+1}`;
        return `
        <div style="background:var(--bg);border:1.5px solid var(--border);border-radius:var(--radius-sm);padding:12px 14px;margin-bottom:10px;">
          <div style="font-size:12px;font-weight:800;color:var(--text);margin-bottom:10px;">
            🌿 ${label} — Nutrients per 100g
          </div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;">
            ${NUTR_KEYS.map(k => `
              <div>
                <label style="font-size:10px;font-weight:700;color:var(--muted);display:block;margin-bottom:3px;">${NUTR_LABELS[k]}</label>
                <input type="number" min="0" step="0.1" value="${n[k]||0}"
                  oninput="validateNutrientInput(this); updateNutrient(${i},'${k}',parseFloat(this.value)||0)"
                  max="100"
                  style="width:100%;padding:6px 8px;border:1.5px solid var(--border);border-radius:5px;font-size:12px;outline:none;font-family:var(--font);" />
              </div>`).join('')}
          </div>
        </div>`;
    }).join('');
}

function validateNutrientInput(input) {
    const value = parseFloat(input.value);
    if (isNaN(value) || value < 0) { input.value = 0; }
    else if (value > 100) { alert("Value must be between 0 and 100"); input.value = 100; }
}

function updateNutrient(ingIdx, key, val) {
    if (!_pkgIngredients[ingIdx]) return;
    if (!_pkgIngredients[ingIdx].nutrients_per_100g)
        _pkgIngredients[ingIdx].nutrients_per_100g = {};
    _pkgIngredients[ingIdx].nutrients_per_100g[key] = val;
}

// ── Save package ───────────────────────────────────────────────
// ── Package image upload ──────────────────────────────────────
let _pkgImageDataUrl = null;  // holds the base64 data URL of newly chosen image
let _pkgImageExisting = null; // existing image filename when editing

function previewPackageImage(input) {
    const file = input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (e) => {
        _pkgImageDataUrl = e.target.result;
        const prev = document.getElementById('pkg-image-preview');
        const img  = document.getElementById('pkg-image-preview-img');
        const fn   = document.getElementById('pkg-image-filename');
        const clr  = document.getElementById('pkg-image-clear');
        img.src = _pkgImageDataUrl;
        prev.style.display = '';
        fn.textContent = file.name;
        clr.style.display = '';
    };
    reader.readAsDataURL(file);
}

function clearPackageImage() {
    _pkgImageDataUrl = null;
    _pkgImageExisting = null;
    const prev = document.getElementById('pkg-image-preview');
    const img  = document.getElementById('pkg-image-preview-img');
    const fn   = document.getElementById('pkg-image-filename');
    const clr  = document.getElementById('pkg-image-clear');
    const fi   = document.getElementById('pkg-image-file');
    img.src = '';
    prev.style.display = 'none';
    fn.textContent = 'No file chosen';
    clr.style.display = 'none';
    if (fi) fi.value = '';
}

function _resetPkgImageUI() {
    _pkgImageDataUrl = null;
    _pkgImageExisting = null;
    clearPackageImage();
}

function _loadPkgImagePreview(imageFilename) {
    _pkgImageExisting = imageFilename || null;
    if (!imageFilename) return;
    const img  = document.getElementById('pkg-image-preview-img');
    const prev = document.getElementById('pkg-image-preview');
    const fn   = document.getElementById('pkg-image-filename');
    const clr  = document.getElementById('pkg-image-clear');
    img.src = `images/${imageFilename}`;
    prev.style.display = '';
    fn.textContent = imageFilename;
    clr.style.display = '';
}

// ── Manual price override ──────────────────────────────────────
function toggleManualPrice(manual) {
    const field = document.getElementById('pkg-price');
    if (!field) return;
    if (manual) {
        field.readOnly = false;
        field.style.background = '';
        field.style.color = 'var(--text)';
        field.placeholder = 'Enter custom price';
    } else {
        field.readOnly = true;
        field.style.background = 'var(--bg)';
        field.style.color = 'var(--muted)';
        field.placeholder = 'Auto-calculated';
        _recalcPkgPrice();
    }
}

// ── Product Picker Modal ───────────────────────────────────────
let _prodPickerHighlight = -1;

function openProductPickerModal() {
    document.getElementById('prod-picker-overlay').classList.add('open');
    const pm = document.getElementById('prod-picker-modal');
    pm.classList.add('open');
    pm.style.setProperty('display', 'flex', 'important');
    document.getElementById('prod-picker-search').value = '';
    _prodPickerHighlight = -1;
    renderProductPicker();
    setTimeout(() => document.getElementById('prod-picker-search').focus(), 80);

    // Keyboard nav
    document.getElementById('prod-picker-modal').onkeydown = function(e) {
        const items = document.querySelectorAll('#prod-picker-list .picker-item');
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            _prodPickerHighlight = Math.min(_prodPickerHighlight + 1, items.length - 1);
            _highlightPickerItem(items);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            _prodPickerHighlight = Math.max(_prodPickerHighlight - 1, 0);
            _highlightPickerItem(items);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (_prodPickerHighlight >= 0 && items[_prodPickerHighlight]) {
                items[_prodPickerHighlight].click();
            }
        } else if (e.key === 'Escape') {
            closeProductPickerModal();
        }
    };
}

function _highlightPickerItem(items) {
    items.forEach((it, i) => {
        it.style.background = i === _prodPickerHighlight ? 'var(--blue-xlt)' : '';
        it.style.borderColor = i === _prodPickerHighlight ? 'var(--blue)' : 'var(--border)';
    });
    if (items[_prodPickerHighlight]) items[_prodPickerHighlight].scrollIntoView({ block: 'nearest' });
}

function closeProductPickerModal() {
    document.getElementById('prod-picker-overlay').classList.remove('open');
    const pm = document.getElementById('prod-picker-modal');
    pm.classList.remove('open');
    pm.style.removeProperty('display');
}

function renderProductPicker() {
    const query = (document.getElementById('prod-picker-search').value || '').toLowerCase().trim();
    const list = document.getElementById('prod-picker-list');
    _prodPickerHighlight = -1;

    // Filter only real products (exclude packages)
    let allProds = (products || []).filter(p => !p.is_package);

    let filtered = allProds;
    if (query) {
        filtered = allProds.filter(p => {
            // Use existing buildSearchCorpus if available, otherwise fallback
            const corpus = (typeof _buildCorpus === 'function')
                ? _buildCorpus(p)
                : [p.name_english, p.name_tamil, p.name_tanglish, p.shortcode, p.category]
                    .filter(Boolean).join(' ').toLowerCase();
            return corpus.includes(query);
        });
    }

    if (!filtered.length) {
        list.innerHTML = '<p style="color:var(--muted);font-size:13px;padding:12px 0;">'
            + (allProds.length === 0 ? '⏳ Products loading…' : 'No products found.') + '</p>';
        return;
    }

    // Store filtered list for index-based access (safe for Tamil names)
    window._pickerFiltered = filtered.slice(0, 80);
    list.innerHTML = window._pickerFiltered.map((p, idx) => {
        // Use correct getProductImage function (not getProductImagePath which takes (nameEn, imgField))
        const imgPath = getProductImage(p);
        const imgHtml = imgPath && imgPath !== 'images/default.webp'
            ? `<img src="${imgPath}" loading="lazy" style="width:42px;height:42px;object-fit:cover;border-radius:6px;flex-shrink:0;" onerror="this.style.display='none'" />`
            : `<div style="width:42px;height:42px;border-radius:6px;background:var(--bg);display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0;">${p.emoji||'🌿'}</div>`;
        return `
        <div class="picker-item" data-idx="${idx}"
          style="display:flex;align-items:center;gap:12px;padding:10px 12px;border:1.5px solid var(--border);border-radius:var(--radius-sm);cursor:pointer;transition:all .12s;background:#fff;">
          ${imgHtml}
          <div style="flex:1;min-width:0;">
            <div style="font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${p.name_english||''}</div>
            <div style="font-size:12px;color:var(--muted);">${p.name_tamil||''} ${p.category ? '· '+p.category : ''}</div>
          </div>
          <div style="text-align:right;flex-shrink:0;">
            <div style="font-size:13px;font-weight:800;color:var(--blue);">₹${p.price_per_kg||0}<small style="font-weight:500;color:var(--muted);font-size:10px;">/kg</small></div>
          </div>
        </div>`;
    }).join('');

    // Attach click events after rendering
    list.querySelectorAll('.picker-item').forEach(el => {
        el.addEventListener('click', () => {
            const idx = parseInt(el.dataset.idx);
            addIngredientFromProduct(window._pickerFiltered[idx]);
        });
    });
}

function addIngredientFromProduct(p) {
    _pkgIngredients.push({
        name_english: p.name_english || '',
        name_tamil:   p.name_tamil   || '',
        ratio_kg:     1,
        price_per_kg: p.price_per_kg || 0,
        _from_product: true,
        nutrients_per_100g: { protein:0, fat:0, carbohydrates:0, fiber:0, moisture:0, ash:0, absorbable_protein:0 }
    });
    closeProductPickerModal();
    renderIngredientRows();
    renderNutrientRows();
    showToast(`✅ "${p.name_english}" added as ingredient`);
}

async function savePackage() {
    const pkgId   = document.getElementById('pkg-edit-id').value.trim();
    const nameEn  = document.getElementById('pkg-name-en').value.trim();
    const nameTa  = document.getElementById('pkg-name-ta').value.trim();
    // Always read the current value (auto-calc fills it even in auto mode)
    const price   = parseFloat(document.getElementById('pkg-price').value) || 0;
    const emoji   = document.getElementById('pkg-emoji').value.trim() || '📦';

    if (!nameEn) { showToast('⚠️ Package name is required'); return; }
    if (!_pkgIngredients.length) { showToast('⚠️ Add at least one ingredient'); return; }

    const productMode = document.getElementById('pkg-product-mode') ? document.getElementById('pkg-product-mode').value : 'normal';
    const payload = {
        name_english: nameEn, name_tamil: nameTa,
        emoji, price_per_kg: price,
        product_mode: productMode,
        nutrition_override: _nutrMode === 'overall' ? {
            protein:            parseFloat(_pkgOverallNutr.protein)            || 0,
            fat:                parseFloat(_pkgOverallNutr.fat)                || 0,
            carbohydrates:      parseFloat(_pkgOverallNutr.carbohydrates)      || 0,
            fiber:              parseFloat(_pkgOverallNutr.fiber)              || 0,
            moisture:           parseFloat(_pkgOverallNutr.moisture)           || 0,
            ash:                parseFloat(_pkgOverallNutr.ash)                || 0,
            absorbable_protein: parseFloat(_pkgOverallNutr.absorbable_protein) || 0,
        } : null,
        items: _pkgIngredients.map(ing => ({
            name_english: ing.name_english || '',
            name_tamil:   ing.name_tamil   || '',
            ratio_kg:     parseFloat(ing.ratio_kg) || 0,
            price_per_kg: parseFloat(ing.price_per_kg) || 0,
            nutrients_per_100g: {
                protein:            parseFloat((ing.nutrients_per_100g||{}).protein)            || 0,
                fat:                parseFloat((ing.nutrients_per_100g||{}).fat)                || 0,
                carbohydrates:      parseFloat((ing.nutrients_per_100g||{}).carbohydrates)      || 0,
                fiber:              parseFloat((ing.nutrients_per_100g||{}).fiber)              || 0,
                moisture:           parseFloat((ing.nutrients_per_100g||{}).moisture)           || 0,
                ash:                parseFloat((ing.nutrients_per_100g||{}).ash)                || 0,
                absorbable_protein: parseFloat((ing.nutrients_per_100g||{}).absorbable_protein) || 0,
            }
        }))
    };

    try {
        const url    = pkgId ? `${API}/admin/packages/${pkgId}` : `${API}/admin/packages`;
        const method = pkgId ? 'PUT' : 'POST';
        const r = await fetch(url, {
            method, headers: {'Content-Type':'application/json'},
            body: JSON.stringify(payload)
        });
        const d = await r.json();
        if (d.success) {
            const savedPkgId = pkgId || d.id;
            // Upload package image if a new one was chosen
            if (_pkgImageDataUrl && savedPkgId) {
                try {
                    await fetch(`${API}/admin/packages/${savedPkgId}/image`, {
                        method: 'POST',
                        headers: {'Content-Type':'application/json'},
                        body: JSON.stringify({ image_data: _pkgImageDataUrl })
                    });
                } catch(imgErr) { /* non-fatal */ }
            }
            showToast(pkgId ? '✅ Package updated!' : '✅ Package created!');
            closePackageForm();
            await loadAdminPackages();
            if (typeof fetchProducts === 'function') fetchProducts();
        } else {
            showToast('⚠️ ' + (d.message || 'Save failed'));
        }
    } catch(e) {
        showToast('⚠️ Network error');
    }
}

// ── Delete package ─────────────────────────────────────────────
async function deletePackage() {
    const pkgId = document.getElementById('pkg-edit-id').value.trim();
    if (!pkgId) return;
    const name = document.getElementById('pkg-name-en').value || pkgId;
    if (!confirm(`Delete package "${name}"? This cannot be undone.`)) return;
    try {
        const r = await fetch(`${API}/admin/packages/${pkgId}`, { method: 'DELETE' });
        const d = await r.json();
        if (d.success) {
            showToast('🗑 Package deleted');
            closePackageForm();
            await loadAdminPackages();
            if (typeof fetchProducts === 'function') fetchProducts();
        } else {
            showToast('⚠️ Delete failed');
        }
    } catch(e) {
        showToast('⚠️ Network error');
    }
}

/* ── Viewport stability fix (pywebview/desktop) ── */
// Layout uses 100vh via CSS — no JS height injection needed for desktop.
// We still export setAppHeight as a no-op so any callers don't break.
function setAppHeight() { /* no-op: CSS uses 100vh — stable in pywebview */ }
// Legacy: keep resize listener but do nothing (avoids errors if called elsewhere)
window.addEventListener('resize', setAppHeight);


// ════════════════════════════════════════════════════
// TAMIL TRANSLITERATION – INIT
// ════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
    // Enable transliteration on Tamil name fields by default
    setTimeout(() => {
        tamilTranslit.enable('adm-name-ta');
        tamilTranslit.enable('edit-name-ta');
    }, 500);
});


/* ─── Collapsible Customer Info (default: collapsed) ─── */
function toggleCustomerInfo() {
  const el = document.getElementById('customer-inputs-collapsible');
  const arrow = document.getElementById('customer-toggle-arrow');
  if (!el) return;
  if (el.style.display === 'none') {
    el.style.display = 'flex';
    if (arrow) arrow.style.transform = 'rotate(180deg)';
  } else {
    el.style.display = 'none';
    if (arrow) arrow.style.transform = 'rotate(0deg)';
  }
}

// ════════════════════════════════════════════════════
// TASK 2: LOCK FOCUS INSIDE MODAL
// ════════════════════════════════════════════════════
document.addEventListener("focusin", (e) => {
    const modal = document.querySelector("#wt-modal.open, #pkg-modal.open");
    if (modal && !modal.contains(e.target)) {
        e.stopPropagation();
        const inp = modal.querySelector("#wt-input") || modal.querySelector("#pkg-user-qty");
        if (inp) inp.focus();
    }
});

// ════════════════════════════════════════════════════
// CREDIT & DUE TRACKING  (v8)
// ════════════════════════════════════════════════════

let _currentPaymentMethod = 'cash';  // 'cash' | 'upi' | 'credit'

function setPaymentMethod(method) {
    _currentPaymentMethod = method;
    ['cash','upi','credit'].forEach(m => {
        const btn = document.getElementById(`pm-${m}`);
        if (btn) btn.classList.toggle('active', m === method);
    });
    const section = document.getElementById('credit-due-section');
    const inp     = document.getElementById('paid-amount-input');
    const cashInp = document.getElementById('cash-input');
    if (method === 'credit') {
        // Credit mode: show paid-amount input; default paid=0 → full due
        if (section) section.style.display = '';
        if (inp && inp.value === '') inp.value = '0';
        // Clear cash input so it doesn't bleed into totals
        if (cashInp) cashInp.value = '';
        // Auto-focus paid input so user can type without mouse
        setTimeout(() => { inp && inp.focus(); inp && inp.select(); }, 50);
    } else {
        // Cash / UPI: hide credit panel, clear partial-paid input
        if (section) section.style.display = 'none';
        if (inp) inp.value = '';
        // Auto-focus cash input for keyboard workflow
        const _ci = document.getElementById('cash-input');
        setTimeout(() => { _ci && _ci.focus(); _ci && _ci.select(); }, 50);
    }
    updateTotals();  // single call — recalculates everything instantly
}

function _getCartTotal() {
    const serverTotal = (window.currentBill && window.currentBill.total_amount != null)
        ? parseFloat(window.currentBill.total_amount) : null;
    const loyaltyDiscount = applyLoyaltyReward ? (_loyaltySettings.discount || 0) : 0;
    const cartSum = cart.reduce((s, i) => s + i.price, 0);
    return serverTotal !== null ? serverTotal : Math.max(0, cartSum - loyaltyDiscount);
}

// calcDue() is now an alias for updateTotals() — defined above

// Reset credit section state. Buttons are always visible in footer;
// only the credit-due-section panel (paid input + due row) needs managing.
function _updateCreditSection() {
    if (cart.length === 0) {
        // Hide credit panel and reset to cash mode
        const section = document.getElementById('credit-due-section');
        if (section) section.style.display = 'none';
        const inp = document.getElementById('paid-amount-input');
        if (inp) inp.value = '';
        const cashInp = document.getElementById('cash-input');
        if (cashInp) cashInp.value = '';
        _currentPaymentMethod = 'cash';
        ['cash','upi','credit'].forEach(m => {
            const btn = document.getElementById('pm-' + m);
            if (btn) btn.classList.toggle('active', m === 'cash');
        });
        updateTotals();  // reset all amount displays to 0
    }
}

// Show due warning when customer has outstanding balance
function _showCustomerDueWarning(totalDue) {
    const warn = document.getElementById('customer-due-warning');
    const amt  = document.getElementById('warning-due-amount');
    if (!warn || !amt) return;
    if (totalDue && totalDue > 0) {
        amt.textContent       = `₹${parseFloat(totalDue).toFixed(2)}`;
        warn.style.display    = '';
    } else {
        warn.style.display    = 'none';
    }
}

// ── lookupCustomer with due-warning support ─────────────────────────────────
function lookupCustomer(phone) {
    clearTimeout(_lookupTimer);
    const cleaned = phone.replace(/\D/g,'');
    const nameEl  = document.getElementById('customer-name');
    if (cleaned.length < 10) {
        if (_nameAutoFilled) { nameEl.value = ''; _nameAutoFilled = false; }
        _hideLoyaltyPanel();
        _latestRepeatBill = null;
        _renderRepeatBillPreview(null);
        _showCustomerDueWarning(0);
        return;
    }
    _lookupTimer = setTimeout(async () => {
        try {
            const r = await fetch(`${API}/customer/${cleaned}`);
            const d = await r.json();
            if (d.found) {
                nameEl.value    = d.name;
                _nameAutoFilled = true;
                showToast(`👤 Found: ${d.name}`);
            } else {
                _nameAutoFilled = false;
            }
            _showLoyaltyPanel(d);
            _showCustomerDueWarning(d.total_due || 0);
            _loadRepeatBillPreview(cleaned);
        } catch {}
    }, 400);
}


// ── Credit payload injected into _saveBillCore via window._creditDueGetPayload ──
window._creditDueGetPayload = function() {
    const total = _getCartTotal();
    let paid;
    if (_currentPaymentMethod === 'credit') {
        // Credit mode: paid = whatever user entered in paid-amount-input (can be 0)
        const paidInput = document.getElementById('paid-amount-input');
        paid = paidInput ? Math.min(Math.max(0, Number(paidInput.value) || 0), total) : 0;
    } else {
        // Cash / UPI: fully paid
        paid = total;
    }
    return {
        paid_amount:    parseFloat(paid.toFixed(2)),
        payment_method: _currentPaymentMethod,
    };
};

// ── AUDIT FIX F2 — idempotent bill save ─────────────────────────────────────
// Every distinct cart+customer+payment state gets ONE client_bill_ref. Pressing
// Print/Save again for the SAME state (e.g. after a failed print) reuses the
// already-saved bill instead of creating a duplicate; the server enforces the
// same rule via a unique index on bills.client_bill_ref (covers lost responses).
let _billSaveInFlight = null;     // Promise while a save request is running
let _billRefState     = null;     // { sig, ref }  — ref reserved for this cart state
let _lastSavedBill    = null;     // { sig, bill } — bill already saved for this state

function _newBillRef() {
    try { if (crypto && crypto.randomUUID) return crypto.randomUUID(); } catch {}
    return 'ref-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
}
function _clearBillSaveState() { _billRefState = null; _lastSavedBill = null; }

/** Cash mode only: amount still owed when the typed cash is below the total.
 *  0 = OK (full / over payment, no cash typed, or not Cash mode). Mirrors the
 *  backend guard in app.py::_save_bill_in_txn. */
function _cashShortfall(method, cash, total) {
    if (method !== 'cash' || !(cash > 0)) return 0;
    const short = Math.round((total - cash) * 100) / 100;
    return short > 0 ? short : 0;
}

async function _saveBillCore({ silent = false } = {}) {
    if (_billSaveInFlight) return _billSaveInFlight;      // double click / F3 spam
    _billSaveInFlight = _saveBillCoreImpl({ silent });
    try { return await _billSaveInFlight; }
    finally { _billSaveInFlight = null; }
}

async function _saveBillCoreImpl({ silent = false } = {}) {
    if (!cart.length) {
        if (!silent) showToast('⚠️ Cart is empty');
        return null;
    }
    const total  = cart.reduce((s, i) => s + i.price, 0);
    // Read paid from the active mode input — single source of truth
    const cash   = _currentPaymentMethod === 'credit'
        ? Math.max(0, Number(document.getElementById('paid-amount-input')?.value) || 0)
        : Math.max(0, Number(document.getElementById('cash-input')?.value) || 0);
    const cname  = document.getElementById('customer-name').value.trim();
    const cphone = document.getElementById('customer-phone').value.trim();

    // ── Cash short-payment block: never save cash < total as PAID ────────────
    // (Estimate includes a cashier-applied loyalty discount; the server re-checks
    //  against the authoritative total.)
    const _estTotal = Math.max(0, total - (applyLoyaltyReward ? (_loyaltySettings.discount || 0) : 0));
    const _short = _cashShortfall(_currentPaymentMethod, cash, _estTotal);
    if (_short > 0) {
        showToast(`❌ Bill NOT saved: cash ₹${cash.toFixed(2)} is less than total ₹${_estTotal.toFixed(2)} ` +
                  `(short ₹${_short.toFixed(2)}). Collect the full amount, or switch to Credit to record the due.`);
        const _ci = document.getElementById('cash-input');
        if (_ci) { _ci.focus(); _ci.select(); }
        return null;
    }

    // ── Credit & Due payload ─────────────────────────────────────────────────
    const creditExtra = window._creditDueGetPayload ? window._creditDueGetPayload() : {};

    const payload = {
        customer_name: cname, customer_phone: cphone, bill_language: billLang,
        customer_name_en: document.getElementById('customer-name')?.value.trim() || '',
        apply_loyalty_reward: applyLoyaltyReward,
        items: cart.flatMap(cartItem => {
            if (cartItem.isPackage) {
                return [{
                    product_id: cartItem.pkg.id,
                    weight_g: cartItem.qty_kg * 1000,
                    is_package: true,
                    package_name_en: cartItem.pkg.name_english || cartItem.pkg.name_en,
                    package_name_ta: cartItem.pkg.name_tamil || cartItem.pkg.name_ta,
                    active_items: cartItem.activeItems.map(it => ({
                        name_ta:            it.name_tamil   || it.name_ta  || '',
                        name_en:            it.name_english || it.name_en  || '',
                        name_tamil:         it.name_tamil   || it.name_ta  || '',
                        name_english:       it.name_english || it.name_en  || '',
                        qty_kg:             it.qty_kg,
                        nutrients_per_100g: it.nutrients_per_100g || {}
                    }))
                }];
            }
            return [{
                product_id: cartItem.product.id, weight_g: cartItem.weight_g,
                // The rate the cashier SAW — backend re-derives it and refuses on mismatch.
                expected_price_per_kg: _getCartItemPricePerKg(cartItem),
                ...(cartItem.temp_price_per_kg != null ? {temp_price_per_kg: cartItem.temp_price_per_kg} : {})
            }];
        }),
        price_mode: pricingMode,          // AUDIT FIX F1: S.Rate / P.Rate / Wholesale
        cash, total,
        ...creditExtra   // paid_amount, payment_method
    };
    // Signature of what the BILL contains (cash tendered / language only affect the printout).
    const { cash: _c, total: _t, bill_language: _l, ...sigPayload } = payload;
    const _sig = JSON.stringify(sigPayload);
    if (_lastSavedBill && _lastSavedBill.sig === _sig) {
        if (!silent) showToast(`ℹ️ Bill #${_lastSavedBill.bill.id} already saved — not saving again`);
        return _lastSavedBill.bill;
    }
    if (_lastSavedBill && !confirm(
            `Bill #${_lastSavedBill.bill.id} was already saved for this cart.\n\n` +
            `The cart or payment has changed since. Save a NEW, separate bill?\n\n` +
            `OK = save new bill   ·   Cancel = keep bill #${_lastSavedBill.bill.id}`)) {
        return _lastSavedBill.bill;
    }
    if (!_billRefState || _billRefState.sig !== _sig) _billRefState = { sig: _sig, ref: _newBillRef() };
    payload.client_bill_ref = _billRefState.ref;
    try {
        const r = await fetch(`${API}/bill`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        const d = await r.json();
        if (d.success) {
            _lastSavedBill = { sig: _sig, bill: d.bill };
            let toastMsg = d.duplicate
                ? `ℹ️ Bill #${d.bill.id} was already saved — not duplicated`
                : `🎉 Bill #${d.bill.id} saved!  \u20b9 ${d.bill.total}`;
            if (d.bill.discount_applied > 0) toastMsg += `  (🎁 -\u20b9 ${d.bill.discount_applied} reward)`;
            const starsEarned = d.bill.loyalty ? d.bill.loyalty.stars_earned : 0;
            if (starsEarned > 0) toastMsg += `  +⭐ star earned!`;
            if (d.bill.due_amount > 0) toastMsg += `  💳 Due: ₹${d.bill.due_amount.toFixed(2)}`;
            if (!silent) showToast(toastMsg);
            currentBill                   = d.bill;
            currentBill.saved             = true;
            const _ns = d.bill.nutrition_summary || {};
            currentBill._nutrition_totals = _ns.totals   || null;
            currentBill._nutrition_summary= _ns;
            currentBill.nutrition_display = d.bill.nutrition_display || null;
            currentBill.nutrition_calc    = d.bill.nutrition_calc    || null;
            currentBill.nutr              = d.bill.nutr || {};
            currentBill.total_amount      = d.bill.total_amount != null ? d.bill.total_amount : d.bill.total;
            currentBill.is_package        = !!d.bill.is_package;
            currentBill.stars             = (d.bill.stars          !== undefined) ? d.bill.stars          : (d.bill.loyalty ? (d.bill.loyalty.stars_after || 0) : 0);
            currentBill.stars_required    = (d.bill.stars_required !== undefined) ? d.bill.stars_required : (d.bill.loyalty ? (d.bill.loyalty.stars_required || 0) : 0);
            currentBill.loyalty_enabled   = (d.bill.loyalty_enabled !== undefined) ? !!d.bill.loyalty_enabled : _loyaltySettings.enabled;
            // ── Reset credit section after save ──────────────────────────────
            const paidInp = document.getElementById('paid-amount-input');
            if (paidInp) paidInp.value = '';
            const _cdsEl = document.getElementById('credit-due-section');
            if (_cdsEl) _cdsEl.style.display = 'none';
            _currentPaymentMethod = 'cash';
            ['cash','upi','credit'].forEach(m => {
                const btn = document.getElementById('pm-' + m);
                if (btn) btn.classList.toggle('active', m === 'cash');
            });
            _showCustomerDueWarning(0);
            // ── STOCK FIX ────────────────────────────────────────────────────
            cart.forEach(cartItem => {
                if (cartItem.isPackage) return;
                const p = products.find(pr => pr.id === cartItem.product.id);
                if (p && p.stock !== null && p.stock !== undefined) {
                    p.stock = Math.max(0, parseFloat(p.stock) - cartItem.weight_g / 1000);
                }
            });
            renderProducts();
            return currentBill;
        } else {
            // Show the real reason (validation / price changed / phone required …)
            showToast('❌ Bill NOT saved: ' + (d.message || 'server error'));
            if (d.message && /price .*changed/i.test(d.message)) {
                try { await fetchProducts(); syncCartPricesWithPricingMode(); } catch {}
            }
            return null;
        }
    } catch {
        // The request may or may not have reached the server. The same
        // client_bill_ref is kept, so retrying cannot create a duplicate.
        showToast('⚠️ Server did not answer – press Print/Save again to retry safely');
        return null;
    }
}

// ── Update customer table rendering with due info + collect button ────────────
async function loadCustomers() {
    const el = document.getElementById('cust-search-results');
    if (!el) return;
    el.innerHTML = '<p style="color:#888;">Loading customers…</p>';
    try {
        const r = await fetch(`${API}/admin/customers?q=`);
        const d = await r.json();
        if (!d.customers || !d.customers.length) {
            el.innerHTML = '<p style="color:#888;padding:10px;">No customers yet</p>'; return;
        }
        _renderCustomerTable(el, d.customers);
    } catch { el.innerHTML = '<p style="color:red;">Server error</p>'; }
}

async function searchCustomers() {
    const q  = document.getElementById('cust-search-input').value.trim();
    if (!q) { loadCustomers(); return; }
    const el = document.getElementById('cust-search-results');
    el.innerHTML = '<p style="color:#888;">Searching…</p>';
    try {
        const r = await fetch(`${API}/admin/customers?q=${encodeURIComponent(q)}`);
        const d = await r.json();
        if (!d.customers || !d.customers.length) {
            el.innerHTML = '<p style="color:#888;padding:10px;">No customers found</p>'; return;
        }
        _renderCustomerTable(el, d.customers);
    } catch { el.innerHTML = '<p style="color:red;">Server error</p>'; }
}

function openAddCustomerModal() {
    const err = document.getElementById('add-cust-error');
    const nameEl = document.getElementById('add-cust-name');
    const phoneEl = document.getElementById('add-cust-phone');
    if (err) { err.style.display = 'none'; err.textContent = ''; }
    if (nameEl) nameEl.value = '';
    if (phoneEl) phoneEl.value = '';
    document.getElementById('add-cust-overlay')?.classList.add('open');
    document.getElementById('add-cust-modal')?.classList.add('open');
    setTimeout(() => nameEl?.focus(), 80);
}

function closeAddCustomerModal() {
    document.getElementById('add-cust-overlay')?.classList.remove('open');
    document.getElementById('add-cust-modal')?.classList.remove('open');
}

function _showAddCustomerError(msg) {
    const err = document.getElementById('add-cust-error');
    if (!err) { showToast('⚠️ ' + msg); return; }
    err.textContent = msg;
    err.style.display = 'block';
}

function _validateNewCustomer(name, phoneDigits) {
    if (!name.trim()) return 'Customer name is required';
    if (!phoneDigits) return 'Phone number is required';
    if (!/^\d+$/.test(phoneDigits)) return 'Phone must contain digits only';
    if (phoneDigits.length < 10 || phoneDigits.length > 15) return 'Phone must be 10–15 digits';
    return null;
}

async function saveNewCustomer() {
    const nameEl = document.getElementById('add-cust-name');
    const phoneEl = document.getElementById('add-cust-phone');
    const name = (nameEl?.value || '').trim();
    const phoneDigits = (phoneEl?.value || '').replace(/\D/g, '');
    const errMsg = _validateNewCustomer(name, phoneDigits);
    if (errMsg) {
        _showAddCustomerError(errMsg);
        showToast('⚠️ ' + errMsg);
        return;
    }
    const err = document.getElementById('add-cust-error');
    if (err) err.style.display = 'none';
    try {
        const r = await fetch(`${API}/admin/customers`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, phone: phoneDigits }),
        });
        const d = await r.json();
        if (!r.ok || !d.success) {
            const msg = d.error || d.message || 'Could not save customer';
            _showAddCustomerError(msg);
            showToast('❌ ' + msg);
            return;
        }
        closeAddCustomerModal();
        showToast(`✅ Customer saved: ${name}`);
        const searchIn = document.getElementById('cust-search-input');
        if (searchIn) searchIn.value = phoneDigits;
        await searchCustomers();
    } catch (e) {
        const msg = 'Server error — could not save customer';
        _showAddCustomerError(msg);
        showToast('⚠️ ' + msg);
    }
}

function _renderCustomerTable(el, customers) {
    const totalDueAll = customers.reduce((s, c) => s + (c.total_due || 0), 0);
    el.innerHTML = `
      <div style="display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap;">
        <div class="credit-stat-card">
          <div class="credit-stat-label">💳 Credit Customers</div>
          <div class="credit-stat-value">${customers.filter(c => (c.total_due||0) > 0).length}</div>
        </div>
        <div class="credit-stat-card">
          <div class="credit-stat-label">💰 Total Outstanding</div>
          <div class="credit-stat-value" style="color:#DC2626;">₹${totalDueAll.toFixed(2)}</div>
        </div>
      </div>
      <table class="detail-table">
        <thead><tr><th>Name</th><th>Phone</th><th>Bills</th><th>Total Due</th><th></th></tr></thead>
        <tbody>
          ${customers.map(c => `
            <tr>
              <td>${c.name || '—'}</td>
              <td>${c.phone}</td>
              <td>${c.bill_count}</td>
              <td style="font-weight:700;color:${(c.total_due||0)>0?'#DC2626':'#16a34a'};">
                ${(c.total_due||0)>0 ? `₹${parseFloat(c.total_due).toFixed(2)}` : '✅ Clear'}
              </td>
              <td class="cust-actions-cell">
                <button type="button" class="btn-cust-bills" data-phone="${c.phone}" data-name="${(c.name || '').replace(/"/g, '&quot;')}">📄 Bills</button>
                ${(c.total_due||0)>0 ? `
                <button type="button" class="btn-cust-collect" data-phone="${c.phone}" data-name="${(c.name || '').replace(/"/g, '&quot;')}" data-due="${parseFloat(c.total_due).toFixed(2)}">💵 Collect</button>` : ''}
                <button type="button" class="btn-cust-delete" style="background:#ef4444; color:white; border:none; border-radius:4px; padding:6px 10px; cursor:pointer; font-size: 13px;" data-phone="${c.phone}" data-name="${(c.name || '').replace(/"/g, '&quot;')}">🗑️ Delete</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>`;
    el.querySelectorAll('.btn-cust-bills').forEach(btn => {
        btn.onclick = () => showCustHistory(btn.dataset.phone, btn.dataset.name || '');
    });
    el.querySelectorAll('.btn-cust-collect').forEach(btn => {
        btn.onclick = () => openCollectPayment(btn.dataset.phone, btn.dataset.name || '', parseFloat(btn.dataset.due) || 0);
    });
    el.querySelectorAll('.btn-cust-delete').forEach(btn => {
        btn.onclick = () => deleteCustomer(btn.dataset.phone, btn.dataset.name || '');
    });
}

async function deleteCustomer(phone, name) {
    if (!confirm(`Are you sure you want to permanently delete customer ${name || phone}?`)) return;
    try {
        const res = await fetch(`${API}/admin/customers/${encodeURIComponent(phone)}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.success) {
            showToast('✅ Customer deleted');
            loadCustomers();
        } else {
            showToast('❌ Failed: ' + data.error);
        }
    } catch {
        showToast('❌ Server error');
    }
}

// ── Override showCustHistory to include due + payment history ─────────────────
async function showCustHistory(phone, name) {
    document.getElementById('cust-hist-title').textContent = `${name} (${phone})`;
    document.getElementById('cust-hist-body').innerHTML = '<p>Loading…</p>';
    document.getElementById('cust-hist-overlay').classList.add('open');
    document.getElementById('cust-hist-modal').classList.add('open');
    try {
        const r = await fetch(`${API}/api/customer/${phone}/payment-history`);
        const d = await r.json();
        const bills   = d.bills   || [];
        const history = d.payment_history || [];
        const due     = parseFloat(d.total_due || 0);

        let html = '';

        // Due summary
        if (due > 0) {
            html += `
              <div style="background:#FEE2E2;border:1px solid #FCA5A5;border-radius:8px;padding:10px 14px;margin-bottom:12px;display:flex;align-items:center;justify-content:space-between;">
                <span style="font-weight:700;color:#991B1B;font-size:14px;">💳 Outstanding Due: ₹${due.toFixed(2)}</span>
                <button onclick="openCollectPayment('${phone}','${name}',${due})"
                  style="padding:6px 14px;background:#DC2626;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:12px;font-weight:700;">
                  💵 Collect Payment</button>
              </div>`;
        } else {
            html += `<div style="background:#DCFCE7;border:1px solid #86EFAC;border-radius:8px;padding:8px 14px;margin-bottom:12px;font-weight:600;color:#166534;font-size:13px;">✅ No outstanding dues</div>`;
        }

        // Payment history
        if (history.length) {
            html += `<h4 style="margin:12px 0 6px;font-size:13px;color:#7C3AED;">💰 Payment History</h4>
              <table class="detail-table" style="margin-bottom:12px;">
                <thead><tr><th>Date</th><th>Amount</th><th>Method</th><th>Note</th></tr></thead>
                <tbody>${history.map(h => `
                  <tr>
                    <td style="font-size:11px;">${h.date}</td>
                    <td style="font-weight:700;color:#16a34a;">₹${parseFloat(h.amount).toFixed(2)}</td>
                    <td>${h.method || 'cash'}</td>
                    <td style="color:#888;">${h.note || '—'}</td>
                  </tr>`).join('')}
                </tbody>
              </table>`;
        }

        // Bills
        if (!bills.length) {
            html += '<p style="color:#888;">No bills found</p>';
        } else {
            html += `<h4 style="margin:12px 0 6px;font-size:13px;color:#7C3AED;">🧾 Bills</h4>`;
            html += bills.map(b => {
                const items    = (typeof b.items === 'string') ? JSON.parse(b.items) : b.items || [];
                const dueAmt   = parseFloat(b.due_amount || 0);
                const status   = b.payment_status || (dueAmt > 0 ? 'CREDIT' : 'PAID');
                const statusCl = status === 'PAID' ? 'color:#16a34a;' : 'color:#DC2626;';
                return `
                  <div class="hist-bill-card">
                    <div class="hist-bill-head">
                      <strong>Bill #${b.id}</strong>
                      <span><span class="currency">₹</span><span class="value">${parseFloat(b.total).toFixed(2)}</span></span>
                      <span style="${statusCl}font-weight:700;font-size:12px;">${status}</span>
                      ${dueAmt > 0 ? `<span style="font-size:11px;color:#DC2626;">Due: ₹${dueAmt.toFixed(2)}</span>` : ''}
                      <span style="color:#888;font-size:11px;">${b.created_at}</span>
                    </div>
                    <table class="detail-table" style="margin-top:6px;">
                      <thead><tr><th>Item</th><th>Qty</th><th>Rate</th><th>Total</th></tr></thead>
                      <tbody>
                        ${items.map(it => `
                          <tr>
                            <td>${_h(it.name)}</td>
                            <td>${it.weight_g>=1000?(it.weight_g/1000).toFixed(2)+'kg':it.weight_g+'g'}</td>
                            <td><span class="currency">₹</span><span class="value">${it.price_per_kg}/kg</span></td>
                            <td><span class="currency">₹</span><span class="value">${parseFloat(it.total_price).toFixed(2)}</span></td>
                          </tr>`).join('')}
                      </tbody>
                    </table>
                  </div>`;
            }).join('');
        }

        document.getElementById('cust-hist-body').innerHTML = html;
    } catch(e) {
        document.getElementById('cust-hist-body').innerHTML = '<p style="color:red;">Error loading data</p>';
    }
}

// ── Collect Payment Modal ─────────────────────────────────────────────────────
let _collectPhone = '', _collectName = '', _collectMaxDue = 0;

function openCollectPayment(phone, name, totalDue) {
    _collectPhone  = phone;
    _collectName   = name;
    _collectMaxDue = parseFloat(totalDue) || 0;

    document.getElementById('cp-customer-name').textContent = `${name} (${phone})`;
    document.getElementById('cp-due-amount').textContent    = `₹${_collectMaxDue.toFixed(2)}`;
    document.getElementById('cp-amount-input').value        = _collectMaxDue.toFixed(2);
    document.getElementById('cp-method-select').value       = 'cash';
    document.getElementById('cp-note-input').value          = '';
    document.getElementById('cp-error-msg').style.display   = 'none';
    document.getElementById('cp-overlay').style.display     = '';
    document.getElementById('cp-modal').style.display       = '';
    document.getElementById('cp-amount-input').focus();
}

function closeCollectPayment() {
    document.getElementById('cp-overlay').style.display = 'none';
    document.getElementById('cp-modal').style.display   = 'none';
}

async function submitCollectPayment() {
    const amount = parseFloat(document.getElementById('cp-amount-input').value) || 0;
    const method = document.getElementById('cp-method-select').value;
    const note   = document.getElementById('cp-note-input').value.trim();
    const errEl  = document.getElementById('cp-error-msg');

    if (amount <= 0) {
        errEl.textContent    = 'Amount must be greater than 0';
        errEl.style.display  = '';
        return;
    }
    if (amount > _collectMaxDue + 0.01) {
        errEl.textContent    = `Amount cannot exceed outstanding due ₹${_collectMaxDue.toFixed(2)}`;
        errEl.style.display  = '';
        return;
    }
    errEl.style.display = 'none';

    try {
        const r = await fetch(`${API}/api/customer/${_collectPhone}/collect-payment`, {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ amount, method, note })
        });
        const d = await r.json();
        if (d.success) {
            showToast(`✅ Payment of ₹${amount.toFixed(2)} recorded for ${_collectName}`);
            closeCollectPayment();
            // Refresh customer list / history if in admin
            if (typeof loadCustomers === 'function') loadCustomers();
            // If history modal was open, refresh it
            const histModal = document.getElementById('cust-hist-modal');
            if (histModal && histModal.classList.contains('open')) {
                showCustHistory(_collectPhone, _collectName);
            }
        } else {
            errEl.textContent   = d.message || 'Payment failed';
            errEl.style.display = '';
        }
    } catch {
        errEl.textContent   = 'Server error — could not record payment';
        errEl.style.display = '';
    }
}

// ── Credit Report tab ─────────────────────────────────────────────────────────
async function loadCreditReport() {
    const el = document.getElementById('adm-credit-panel');
    if (!el) return;
    el.innerHTML = '<p style="color:#888;padding:20px;">Loading report…</p>';
    try {
        const r = await fetch(`${API}/api/credit-report`);
        const d = await r.json();
        const customers = d.credit_customers || [];
        el.innerHTML = `
          <div class="admin-card">
            <h3>📊 Credit Report</h3>
            <div style="display:flex;gap:16px;margin-bottom:16px;flex-wrap:wrap;">
              <div class="credit-stat-card">
                <div class="credit-stat-label">💳 Credit Customers</div>
                <div class="credit-stat-value">${d.total_credit_customers}</div>
              </div>
              <div class="credit-stat-card">
                <div class="credit-stat-label">💰 Total Outstanding</div>
                <div class="credit-stat-value" style="color:#DC2626;">₹${parseFloat(d.total_outstanding_amount||0).toFixed(2)}</div>
              </div>
            </div>
            ${customers.length === 0
              ? '<p style="color:#888;">No outstanding dues 🎉</p>'
              : `<table class="detail-table">
                  <thead><tr><th>Customer</th><th>Phone</th><th>Credit Bills</th><th>Total Due</th><th></th></tr></thead>
                  <tbody>
                    ${customers.map(c => `
                      <tr>
                        <td>${c.name || '—'}</td>
                        <td>${c.phone}</td>
                        <td>${c.credit_bills}</td>
                        <td style="font-weight:800;color:#DC2626;">₹${parseFloat(c.total_due).toFixed(2)}</td>
                        <td>
                          <button onclick='openCollectPayment(${_h(JSON.stringify(String(c.phone)))},${_h(JSON.stringify(String(c.name || "")))},${parseFloat(c.total_due).toFixed(2)})'
                            style="padding:4px 10px;background:#DC2626;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px;font-weight:700;">
                            💵 Collect</button>
                        </td>
                      </tr>`).join('')}
                  </tbody>
                </table>`}
          </div>`;
    } catch { el.innerHTML = '<p style="color:red;">Error loading report</p>'; }
}

// Initialise credit section visibility when page loads
document.addEventListener('DOMContentLoaded', () => {
    _updateCreditSection();
});

// ════════════════════════════════════════════════════
// PRODUCTION PATCHES — Issues 3 & 4
// ════════════════════════════════════════════════════
// This block is appended LAST so it cleanly overrides earlier definitions
// without touching any working logic above.
//
// Fixes:
//   • Issue 4: Define missing closeCustHistory() (the original was referenced
//     but never declared — ESC + ✕ + overlay clicks were all silently failing).
//   • Issue 4: Replace ModalManager with a true LIFO open-time stack so ESC
//     always closes the most-recently-opened modal regardless of focus
//     (critical inside pywebview where focus is unreliable).
//   • Issue 3: Add a "🔁 Repeat" button on every bill row in the customer
//     history modal + a clean structured summary table (Bill # | Date | Total).
//   • Issue 3: repeatBillById() loads any past bill into the current cart
//     without leaving the screen, ready for editing before saving.
// ════════════════════════════════════════════════════
(function _productionPatches() {

    // ── 1. closeCustHistory: graceful close + ModalManager pop ───────────────
    if (typeof window.closeCustHistory !== 'function') {
        window.closeCustHistory = function closeCustHistory() {
            const ov = document.getElementById('cust-hist-overlay');
            const md = document.getElementById('cust-hist-modal');
            if (ov) ov.classList.remove('open');
            if (md) md.classList.remove('open');
            if (window.ModalManager && typeof ModalManager._notifyClosed === 'function') {
                ModalManager._notifyClosed('cust-hist-modal');
            }
        };
    }

    // ── 2. True LIFO Modal Stack ─────────────────────────────────────────────
    // Replaces the priority-based stack with an open-order stack. Each modal
    // is auto-pushed when its element gains the `.open` class and popped when
    // it loses it. ESC always closes the top (last-opened) one.
    const _knownModals = new Map(); // id -> closeFn
    const _openStack   = [];        // array of ids in open order (top = last)

    function _registerModal(id, closeFn) {
        _knownModals.set(id, closeFn);
        const el = document.getElementById(id);
        if (!el) return;
        // Watch the .open class to maintain the stack automatically
        const obs = new MutationObserver(() => {
            const isOpen = el.classList.contains('open');
            const idx    = _openStack.indexOf(id);
            if (isOpen && idx === -1)      _openStack.push(id);
            else if (!isOpen && idx !== -1) _openStack.splice(idx, 1);
        });
        obs.observe(el, { attributes: true, attributeFilter: ['class'] });
        // Seed initial state
        if (el.classList.contains('open') && _openStack.indexOf(id) === -1) {
            _openStack.push(id);
        }
    }

    function _closeTopModal() {
        // Pop from the end (most recently opened still-open modal)
        for (let i = _openStack.length - 1; i >= 0; i--) {
            const id = _openStack[i];
            const el = document.getElementById(id);
            if (el && el.classList.contains('open')) {
                const fn = _knownModals.get(id);
                if (typeof fn === 'function') {
                    try { fn(); } catch (err) { console.error('[ModalManager] close error', id, err); }
                }
                // Defensive: ensure removed from stack even if closeFn forgot
                const idx = _openStack.indexOf(id);
                if (idx !== -1) _openStack.splice(idx, 1);
                return true;
            }
        }
        return false;
    }

    // Replace the previously installed ModalManager
    window.ModalManager = {
        register: (id, closeFn /*, priority ignored */) => _registerModal(id, closeFn),
        unregister: (id) => {
            _knownModals.delete(id);
            const idx = _openStack.indexOf(id);
            if (idx !== -1) _openStack.splice(idx, 1);
        },
        closeTop: _closeTopModal,
        anyOpen: () => _openStack.length > 0,
        openCount: () => _openStack.length,
        // Internal: lets close functions notify the stack synchronously
        _notifyClosed: (id) => {
            const idx = _openStack.indexOf(id);
            if (idx !== -1) _openStack.splice(idx, 1);
        },
        _debug: () => _openStack.slice(),
    };

    // Re-register every known modal under the new manager
    const _modalCloseHandlers = {
        'wt-modal':          () => typeof closeWeightModal   === 'function' && closeWeightModal(),
        'pkg-modal':         () => typeof closePackageModal  === 'function' && closePackageModal(),
        'edit-modal':        () => typeof closeEditModal     === 'function' && closeEditModal(),
        'bill-detail-modal': () => typeof closeBillDetail    === 'function' && closeBillDetail(),
        'cust-hist-modal':   () => typeof closeCustHistory   === 'function' && closeCustHistory(),
        'price-mode-modal':  () => typeof cancelPriceModeChange === 'function' && cancelPriceModeChange(),
        'cp-modal':          () => typeof closeCollectPayment === 'function' && closeCollectPayment(),
    };

    function _doRegister() {
        Object.entries(_modalCloseHandlers).forEach(([id, fn]) => {
            if (document.getElementById(id)) _registerModal(id, fn);
        });
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _doRegister);
    } else {
        _doRegister();
    }

    // ── 3. Repeat-bill from any past bill ────────────────────────────────────
    // Uses GET /bill/<id> to fetch the full item list, then reuses the
    // existing _rebuildCartFromPreviousBill() helper to populate the cart.
    window.repeatBillById = async function repeatBillById(billId) {
        if (!billId) return;
        try {
            if (cart.length && !confirm('Replace current cart with bill #' + billId + '?')) return;


            const r = await fetch(`${API}/bill/${encodeURIComponent(billId)}`);
            const d = await r.json();
            if (!d || !d.success) {
                showToast(d && d.message ? d.message : 'Could not load bill');
                return;
            }

            // ── STEP 1: Full state reset before loading new bill data ──────────
            // Clear cart array completely — do NOT mutate, assign fresh array
            cart = [];
            currentBill = null;
            applyLoyaltyReward = false;
            _latestRepeatBill  = null;
            _nameAutoFilled    = false;

            // Reset customer fields to blank before filling with repeat data
            const phoneEl = document.getElementById('customer-phone');
            const nameEl  = document.getElementById('customer-name');
            if (phoneEl) phoneEl.value = '';
            if (nameEl)  nameEl.value  = '';

            // Reset payment method to cash
            _currentPaymentMethod = 'cash';
            ['cash','upi','credit'].forEach(m => {
                const btn = document.getElementById('pm-' + m);
                if (btn) btn.classList.toggle('active', m === 'cash');
            });
            const paidInput = document.getElementById('paid-amount-input');
            const cashInput = document.getElementById('cash-input');
            if (paidInput) paidInput.value = '';
            if (cashInput) cashInput.value = '';

            // ── STEP 2: Rebuild cart from fetched bill data ───────────────────
            const { rebuilt, missing } = _rebuildCartFromPreviousBill({
                items: d.items,
                customer_phone: d.customer_phone,
                customer_name:  d.customer_name,
            });
            if (!rebuilt.length) {
                showToast('Bill items could not be restored');
                return;
            }

            // ── STEP 3: Assign new state ──────────────────────────────────────
            cart = rebuilt;

            if (phoneEl && d.customer_phone) phoneEl.value = d.customer_phone;
            if (nameEl  && d.customer_name)  { nameEl.value = d.customer_name; _nameAutoFilled = true; }

            // ── STEP 4: Render fresh — renderCart then updateTotals ──────────
            // renderCart redraws the item list; updateTotals recalculates footer
            renderCart();
            updateTotals();

            // Close history modal + navigate to billing tab
            closeCustHistory();
            // ── CRITICAL: switch to billing page so cart is visible ──────────
            if (typeof setTab === 'function') setTab('billing');
            focusSearch();

            // pywebview repaint nudge — forces layout recalculation
            requestAnimationFrame(() => {
                window.dispatchEvent(new Event('resize'));
            });

            if (missing.length) {
                showToast(`🔁 Bill #${billId} loaded (${missing.length} item${missing.length>1?'s':''} unavailable)`);
            } else {
                showToast(`🔁 Bill #${billId} loaded — edit and save`);
            }
        } catch (err) {
            showToast('Could not load bill');
        }
    };

    // ── 4. Patch showCustHistory: structured rows + expandable detail ────────
    // Each summary row is clickable to lazily load and preview that bill's
    // items inline. The Repeat button is isolated via stopPropagation so it
    // never triggers the expand. Accordion behavior: opening one closes any
    // other. ESC closes an open detail before closing the modal itself.
    const _billDetailCache = new Map(); // billId -> items[]
    let _expandedBillId = null;

    function _escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function _fmtMoney(n) {
        const v = parseFloat(n);
        return isFinite(v) ? v.toFixed(2) : '0.00';
    }

    function _fmtQty(it) {
        // Prefer kg if >= 1, otherwise grams; packages show as "1 pkg"
        if (it.is_package) return '1 pkg';
        const kg = parseFloat(it.qty_kg || 0);
        const g  = parseFloat(it.weight_g || (kg * 1000));
        if (kg >= 1) return kg.toFixed(kg % 1 === 0 ? 0 : 3) + ' kg';
        if (g > 0)   return Math.round(g) + ' g';
        return '-';
    }

    function _renderBillItemsTable(items) {
        if (!items || !items.length) {
            return `<div class="bill-detail-empty">No items found for this bill.</div>`;
        }
        const rows = items.map(it => {
            const name  = _escapeHtml(it.name || it.name_english || '');
            const tamil = it.name_tamil ? `<div class="bd-tamil">${_escapeHtml(it.name_tamil)}</div>` : '';
            const qty   = _escapeHtml(_fmtQty(it));
            const rate  = _fmtMoney(it.price_per_kg);
            const total = _fmtMoney(it.total_price);
            return `
                <tr>
                    <td><div class="bd-name">${name}</div>${tamil}</td>
                    <td style="text-align:center;">${qty}</td>
                    <td style="text-align:right;">₹${rate}</td>
                    <td style="text-align:right;font-weight:700;">₹${total}</td>
                </tr>`;
        }).join('');
        const grand = items.reduce((s, it) => s + (parseFloat(it.total_price) || 0), 0);
        return `
            <table class="bill-detail-table">
                <thead>
                    <tr>
                        <th style="text-align:left;">Item</th>
                        <th style="text-align:center;">Qty</th>
                        <th style="text-align:right;">Rate</th>
                        <th style="text-align:right;">Total</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
                <tfoot>
                    <tr>
                        <td colspan="3" style="text-align:right;font-weight:700;">Grand Total</td>
                        <td style="text-align:right;font-weight:800;color:#7C3AED;">₹${_fmtMoney(grand)}</td>
                    </tr>
                </tfoot>
            </table>`;
    }

    function _collapseBillDetail() {
        if (!_expandedBillId) return false;
        const detail = document.querySelector(`tr.bill-detail-row[data-bill-id="${CSS.escape(_expandedBillId)}"]`);
        const summary = document.querySelector(`tr.bill-summary-row[data-bill-id="${CSS.escape(_expandedBillId)}"]`);
        if (detail) detail.remove();
        if (summary) summary.classList.remove('expanded');
        _expandedBillId = null;
        return true;
    }

    window.toggleBillDetail = async function toggleBillDetail(billId) {
        billId = String(billId);
        // Same row clicked → collapse
        if (_expandedBillId === billId) {
            _collapseBillDetail();
            return;
        }
        // Accordion: close any other open detail first
        if (_expandedBillId) _collapseBillDetail();

        const summary = document.querySelector(`tr.bill-summary-row[data-bill-id="${CSS.escape(billId)}"]`);
        if (!summary) return;
        summary.classList.add('expanded');

        // Insert a placeholder detail row immediately for snappy UX
        const detailRow = document.createElement('tr');
        detailRow.className = 'bill-detail-row';
        detailRow.setAttribute('data-bill-id', billId);
        detailRow.innerHTML = `
            <td colspan="4" class="bill-detail-cell">
                <div class="bill-detail-wrap">
                    <div class="bill-detail-loading">Loading bill #${_escapeHtml(billId)}…</div>
                </div>
            </td>`;
        summary.parentNode.insertBefore(detailRow, summary.nextSibling);
        _expandedBillId = billId;

        // Lazy-load (with cache)
        let items = _billDetailCache.get(billId);
        if (!items) {
            try {
                const r = await fetch(`${API}/bill/${encodeURIComponent(billId)}`);
                const d = await r.json();
                if (!d || !d.success) {
                    items = null;
                    detailRow.querySelector('.bill-detail-wrap').innerHTML =
                        `<div class="bill-detail-empty">${_escapeHtml((d && d.message) || 'Could not load bill')}</div>`;
                    return;
                }
                items = d.items || [];
                _billDetailCache.set(billId, items);
            } catch (err) {
                detailRow.querySelector('.bill-detail-wrap').innerHTML =
                    `<div class="bill-detail-empty">Network error loading bill.</div>`;
                return;
            }
        }
        // Race-guard: ensure user didn't collapse before fetch returned
        if (_expandedBillId !== billId) return;
        const wrap = detailRow.querySelector('.bill-detail-wrap');
        if (wrap) wrap.innerHTML = _renderBillItemsTable(items);
    };

    // ESC closes an open detail before falling through to the modal
    document.addEventListener('keydown', function _escCloseBillDetail(e) {
        if (e.key === 'Escape' && _expandedBillId) {
            e.stopPropagation();
            _collapseBillDetail();
        }
    }, true); // capture phase → runs before ModalManager's listener

    const _origShow = window.showCustHistory;
    if (typeof _origShow === 'function') {
        window.showCustHistory = async function showCustHistoryPatched(phone, name) {
            _expandedBillId = null;
            await _origShow(phone, name);
            try {
                const body = document.getElementById('cust-hist-body');
                if (!body) return;

                const r = await fetch(`${API}/customer-bills/${encodeURIComponent(phone)}`);
                const d = await r.json();
                const bills = (d && d.bills) || [];
                if (!bills.length) return;

                const summaryHtml = `
                  <div class="cust-hist-summary">
                    <h4 class="cust-hist-summary-title">📋 Bill Summary <span class="cust-hist-hint">(click a row to view items)</span></h4>
                    <table class="bill-summary-table">
                      <thead>
                        <tr>
                          <th style="text-align:left;">Bill #</th>
                          <th style="text-align:left;">Date</th>
                          <th style="text-align:right;">Total</th>
                          <th style="text-align:center;">Action</th>
                        </tr>
                      </thead>
                      <tbody>
                        ${bills.map(b => {
                            const id = String(b.bill_number);
                            const safeId = _escapeHtml(id);
                            const jsId = id.replace(/'/g, "\\'");
                            return `
                              <tr class="bill-summary-row" data-bill-id="${safeId}"
                                  onclick="toggleBillDetail('${jsId}')"
                                  title="Click to view items">
                                <td><strong>#${safeId}</strong> <span class="bd-caret">▸</span></td>
                                <td class="bd-date">${_escapeHtml(b.date || '')}</td>
                                <td style="text-align:right;font-weight:700;">
                                  <span class="currency">₹</span><span class="value">${_fmtMoney(b.total_amount)}</span>
                                </td>
                                <td style="text-align:center;white-space:nowrap;">
                                  <button type="button"
                                    onclick="event.stopPropagation(); repeatBillById('${jsId}')"
                                    class="btn-repeat-bill-row">
                                    🔁 Repeat
                                  </button>
                                  <button type="button"
                                    onclick="event.stopPropagation(); reprintBillById('${jsId}')"
                                    class="btn-repeat-bill-row" style="margin-left:4px;">
                                    🖨️
                                  </button>
                                  <button type="button"
                                    onclick="event.stopPropagation(); openAdminBillPreview('${jsId}', 'en')"
                                    class="btn-repeat-bill-row btn-preview-bill-row" style="margin-left:4px;">
                                    Preview
                                  </button>
                                  <button type="button"
                                    onclick="event.stopPropagation(); openAdminBillPreview('${jsId}', 'ta')"
                                    class="btn-repeat-bill-row btn-preview-tamil-row" style="margin-left:4px;">
                                    தமிழ்
                                  </button>
                                </td>
                              </tr>`;
                        }).join('')}
                      </tbody>
                    </table>
                  </div>`;
                body.insertAdjacentHTML('afterbegin', summaryHtml);
            } catch (err) {
            }
        };
    }

    // Reset expanded state when the history modal closes
    const _origCloseHist = window.closeCustHistory;
    window.closeCustHistory = function closeCustHistoryPatched() {
        _expandedBillId = null;
        if (typeof _origCloseHist === 'function') return _origCloseHist();
    };

})();

// ════════════════════════════════════════════════════
// OVERLAY / MODAL CLEANUP SYSTEM  (pywebview fix)
// Prevents white overlay layer, orphan backdrops, and
// scroll-lock bleed after repeated modal usage.
// ════════════════════════════════════════════════════
(function _installOverlayGuard() {

    // All known overlay element IDs (class-toggled with .open)
    var _OVERLAY_IDS   = ['wt-overlay','pkg-overlay','edit-overlay',
                          'bill-detail-overlay','cust-hist-overlay','price-mode-overlay'];
    // Style-toggled overlays (display:none / '')
    var _STYLE_OVERLAYS = ['cp-overlay'];

    // Remove orphan backdrops / desync'd overlays
    function _removeOrphanBackdrops() {
        // Bootstrap-style backdrops
        document.querySelectorAll('.modal-backdrop').forEach(function(el) { el.remove(); });

        // Class-based overlays: open without their matching modal open
        _OVERLAY_IDS.forEach(function(id) {
            var el = document.getElementById(id);
            if (!el || !el.classList.contains('open')) return;
            var modalId = id.replace('-overlay', '-modal');
            var modal   = document.getElementById(modalId);
            if (modal && !modal.classList.contains('open')) {
                el.classList.remove('open');
            }
        });

        // Style-based overlays: visible without matching modal visible
        _STYLE_OVERLAYS.forEach(function(id) {
            var el = document.getElementById(id);
            if (!el || el.style.display === 'none' || el.style.display === '') return;
            var modalId = id.replace('-overlay', '-modal');
            var modal   = document.getElementById(modalId);
            if (modal && modal.style.display === 'none') {
                el.style.display = 'none';
            }
        });

        // Count open overlays for debug
        var openCount = _OVERLAY_IDS.filter(function(id) {
            var el = document.getElementById(id);
            return el && el.classList.contains('open');
        }).length + _STYLE_OVERLAYS.filter(function(id) {
            var el = document.getElementById(id);
            return el && el.style.display !== 'none' && el.style.display !== '';
        }).length;
    }

    // Restore body scroll if all modals are closed
    function _restoreBodyScroll() {
        var anyOpen = _OVERLAY_IDS.some(function(id) {
            var el = document.getElementById(id);
            return el && el.classList.contains('open');
        }) || _STYLE_OVERLAYS.some(function(id) {
            var el = document.getElementById(id);
            return el && el.style.display !== 'none' && el.style.display !== '';
        });

        if (!anyOpen) {
            document.body.classList.remove('modal-open');
            // Only clear inline overflow overrides; the app sets overflow:hidden
            // on body by default via CSS — do not touch the CSS rule itself.
            if (document.body.style.overflow && document.body.style.overflow !== 'hidden') {
                document.body.style.overflow = '';
            }
        }
    }

    // Patch a close function to run cleanup afterwards (once)
    function _patchClose(fnName) {
        // Attempt to patch — retry once after DOMContentLoaded if not yet defined
        function _doPath() {
            if (typeof window[fnName] !== 'function') return;
            var _orig = window[fnName];
            window[fnName] = function() {
                var result = _orig.apply(this, arguments);
                setTimeout(function() {
                    _removeOrphanBackdrops();
                    _restoreBodyScroll();
                    // pywebview repaint nudge — forces compositor update
                    window.dispatchEvent(new Event('resize'));
                }, 60);
                return result;
            };
        }
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', _doPath);
        } else {
            _doPath();
        }
    }

    ['closeWeightModal','closePackageModal','closeEditModal',
     'closeBillDetail','closeCustHistory','cancelPriceModeChange',
     'closeCollectPayment'].forEach(_patchClose);

    // Periodic guard: scan every 3s — only acts when it finds orphans
    var _guardId = null;
    function _startGuard() {
        if (_guardId) return;
        _guardId = setInterval(function() {
            var dirty = false;

            _OVERLAY_IDS.forEach(function(id) {
                var el = document.getElementById(id);
                if (!el || !el.classList.contains('open')) return;
                var modalId = id.replace('-overlay', '-modal');
                var modal   = document.getElementById(modalId);
                if (modal && !modal.classList.contains('open')) {
                    el.classList.remove('open');
                    dirty = true;
                }
            });

            _STYLE_OVERLAYS.forEach(function(id) {
                var el = document.getElementById(id);
                if (!el || el.style.display === 'none' || el.style.display === '') return;
                var modalId = id.replace('-overlay', '-modal');
                var modal   = document.getElementById(modalId);
                if (modal && modal.style.display === 'none') {
                    el.style.display = 'none';
                    dirty = true;
                }
            });

            if (dirty) {
                _restoreBodyScroll();
                window.dispatchEvent(new Event('resize'));
            }
        }, 3000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _startGuard);
    } else {
        _startGuard();
    }

    // Debug helpers accessible from pywebview console
    window._overlayGuard = {
        scan:   _removeOrphanBackdrops,
        scroll: _restoreBodyScroll,
        status: function() {
            var open = _OVERLAY_IDS.filter(function(id) {
                var el = document.getElementById(id); return el && el.classList.contains('open');
            });
            var styleOpen = _STYLE_OVERLAYS.filter(function(id) {
                var el = document.getElementById(id);
                return el && el.style.display !== 'none' && el.style.display !== '';
            });
            if (window.ModalManager && window.ModalManager._debug) {
            }
        },
    };

})();

// ════════════════════════════════════════════════════
// END OF SCRIPT
// ════════════════════════════════════════════════════


// ════════════════════════════════════════════════════
// SIMPLE THERMAL BILL — Bill Type toggle (Normal / Estimate)
// + helper to open server-rendered template (simple_bill_*.html)
// ════════════════════════════════════════════════════
window.currentBillType = window.currentBillType || 'normal';

function setBillType(type) {
  window.currentBillType = (type === 'estimate') ? 'estimate' : 'normal';
  try { localStorage.setItem('billType', window.currentBillType); } catch (e) {}
  const nb = document.getElementById('btype-normal');
  const eb = document.getElementById('btype-estimate');
  if (nb && eb) {
    nb.classList.toggle('active', window.currentBillType === 'normal');
    eb.classList.toggle('active', window.currentBillType === 'estimate');
  }
}

// Restore last selection on load
try {
  const saved = localStorage.getItem('billType');
  if (saved === 'estimate' || saved === 'normal') {
    document.addEventListener('DOMContentLoaded', () => setBillType(saved));
  }
} catch (e) {}

// ════════════════════════════════════════════════════
// THERMAL PRINTER — direct ESC/POS (no browser dialog)
// ════════════════════════════════════════════════════
let _printerSettings = { mode: 'escpos', chars_per_line: 48, paper_width_mm: 80 };

async function loadPrinterSettings() {
    try {
        const r = await fetch('/api/printer/settings');
        const d = await r.json();
        if (d.success && d.settings) _printerSettings = d.settings;
    } catch (e) {
        console.warn('Printer settings load failed', e);
    }
    return _printerSettings;
}

function _resolveBillLang() {
    const _lang = (typeof billLang !== 'undefined' ? billLang
                : (window.currentBillLang || window.billLang || 'en'));
    return (_lang === 'ta' || _lang === 'tamil') ? 'ta' : 'en';
}

async function printViaQZTray(rawBase64, printerName) {
    if (!window.QZTrayPrint) {
        throw new Error('QZ Tray helper not loaded');
    }
    await window.QZTrayPrint.printRawEscpos(rawBase64, printerName || undefined);
}

async function directThermalPrint(billId, paidAmount) {
    if (!billId) return false;
    await loadPrinterSettings();
    const lang = _resolveBillLang();
    const btype = window.currentBillType || 'normal';
    const body = {
        bill_id: billId,
        language: lang,
        bill_type: btype,
    };
    if (paidAmount != null && !isNaN(parseFloat(paidAmount))) {
        body.paid_amount = parseFloat(paidAmount);
    }
    try {
        const res = await fetch('/api/thermal/print', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        window._lastPrintMode = data && data.mode ? data.mode : null;
        if (data.success && data.mode === 'escpos') {
            showToast('🖨️ Sent to printer');
            return true;
        }
        if (data.success && data.mode === 'qz' && data.raw_base64) {
            try {
                await printViaQZTray(data.raw_base64, data.printer_name);
                showToast('🖨️ Sent to QZ Tray');
                return true;
            } catch (qzErr) {
                showToast('⚠️ QZ Tray: ' + (qzErr.message || qzErr) + ' — install QZ Tray from qz.io');
                return false;
            }
        }
        if (data.success && data.mode === 'preview') {
            // Use the same HTML template as browser print for pixel-perfect preview
            const previewUrl = `/preview-thermal/${encodeURIComponent(billId)}?language=${lang}&bill_type=${btype}&paid_amount=${paidAmount || 0}&preview=1`;
            showReceiptPreviewModal(previewUrl, lang);
            // Preview mode is not an actual print; do NOT auto-reset POS.
            return false;
        }
        if (data.mode === 'browser' && data.print_url) {
            openBillPreview(billId, paidAmount);
            return false;
        }
        showToast('⚠️ Print failed: ' + (data.message || 'Unknown error'));
        return false;
    } catch (e) {
        showToast('⚠️ Print error: ' + (e.message || e));
        return false;
    }
}

async function reprintBillById(billId, paidAmount) {
    if (!billId) return;
    showToast('🖨️ Reprinting…');
    await directThermalPrint(String(billId), paidAmount);
}

function reprintBillFromDetail() {
    const id = window._detailBillId;
    if (!id) return;
    const bill = (window._allBills || []).find(b => b.id === id);
    const paid = bill ? (parseFloat(bill.paid_amount || bill.total || bill.cash) || null) : null;
    reprintBillById(id, paid);
}

window.reprintBillById = reprintBillById;

function openBillPreview(billId, paidAmount) {
    if (!billId) return;
    const lang = _resolveBillLang();
    const btype = window.currentBillType || 'normal';
    const paidParam = (paidAmount != null && !isNaN(parseFloat(paidAmount)))
        ? `&paid_amount=${parseFloat(paidAmount).toFixed(2)}` : '';
    const url = `/print-bill/${encodeURIComponent(billId)}`
        + `?language=${lang}&bill_type=${btype}${paidParam}&preview=1&pos_reset=1`;
    window.open(url, '_blank', 'width=420,height=720,scrollbars=yes');
}

function openAdminBillPreview(billId, language = 'en') {
    if (!billId) return;
    const lang = (language === 'ta' || language === 'tamil') ? 'ta' : 'en';
    const url = `/preview-thermal/${encodeURIComponent(billId)}`
        + `?language=${lang}&bill_type=normal`;
    window.open(url, '_blank', 'width=420,height=760,scrollbars=yes');
}
window.openAdminBillPreview = openAdminBillPreview;

function showReceiptPreviewModal(url, language = 'en') {
    // Remove any existing modal
    const existing = document.getElementById('receipt-preview-modal');
    if (existing) existing.remove();

    const el = document.createElement('div');
    el.id = 'receipt-preview-modal';
    el.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99999;display:flex;align-items:center;justify-content:center;padding:20px;';
    el.innerHTML = `
      <div style="background:#fff;border-radius:12px;width:520px;max-width:96vw;height:85vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.4);">
        <div style="padding:12px 16px;border-bottom:1px solid #eee;font-weight:700;display:flex;align-items:center;justify-content:space-between;">
          <span>Receipt Preview</span>
          <button onclick="document.getElementById('receipt-preview-modal').remove()" style="background:none;border:none;font-size:18px;cursor:pointer;color:#666;line-height:1;">&#x2715;</button>
        </div>
        <iframe id="receipt-preview-frame"
          src="${url}"
          style="flex:1;border:none;background:#f5f5f5;"
          title="Receipt Preview">
        </iframe>
        <button onclick="document.getElementById('receipt-preview-modal').remove()" style="margin:12px;padding:10px;border:none;background:#7C3AED;color:#fff;border-radius:8px;font-weight:700;cursor:pointer;font-size:14px;">Close</button>
      </div>`;
    document.body.appendChild(el);
    // Close on backdrop click
    el.addEventListener('click', (e) => { if (e.target === el) el.remove(); });
}



async function renderPrinterAdmin() {
    const panel = document.getElementById('adm-printer-panel');
    if (!panel) return;
    const data = await fetch('/api/printer/settings').then(r => r.json()).catch(() => ({}));
    const s = data.settings || _printerSettings;
    const printers = (data.printers || []).map(p =>
        `<option value="${p.replace(/"/g, '&quot;')}" ${s.printer_name === p ? 'selected' : ''}>${p}</option>`
    ).join('');
    const usbOpts = (data.usb_devices || []).map(d =>
        `<option value="${d.vendor_id}:${d.product_id}">${d.label || (d.vendor_id + ':' + d.product_id)}</option>`
    ).join('');

    panel.innerHTML = `
    <div class="admin-card">
      <h3>🖨️ Thermal Printer Settings</h3>
      <p style="color:#888;font-size:12px;margin-bottom:14px;">
        Direct ESC/POS printing — no browser print dialog. Configure your Windows thermal printer name below.
      </p>
      <div class="admin-form" style="grid-template-columns:1fr 1fr;">
        <div class="form-group">
          <label>Print Mode</label>
          <select id="prn-mode">
            <option value="escpos" ${s.mode==='escpos'?'selected':''}>ESC/POS Direct (recommended)</option>
            <option value="qz" ${s.mode==='qz'?'selected':''}>QZ Tray (raw ESC/POS)</option>
            <option value="preview" ${s.mode==='preview'?'selected':''}>Preview Only</option>
            <option value="browser" ${s.mode==='browser'?'selected':''}>Browser Preview (fallback)</option>
          </select>
        </div>
        <div class="form-group">
          <label>Windows Printer (ESC/POS)</label>
          <select id="prn-name"><option value="">— Select —</option>${printers}</select>
        </div>
        <div class="form-group">
          <label>QZ Tray Printer Name (optional)</label>
          <input type="text" id="prn-qz-name" placeholder="Same as Windows name if blank" value="${(s.qz_printer_name || '').replace(/"/g,'&quot;')}" />
        </div>
        <div class="form-group">
          <label>Paper Width (mm)</label>
          <select id="prn-width">
            <option value="58" ${s.paper_width_mm==58?'selected':''}>58mm</option>
            <option value="80" ${s.paper_width_mm==80?'selected':''}>80mm</option>
          </select>
        </div>
        <div class="form-group">
          <label>Characters Per Line</label>
          <input type="number" id="prn-chars" min="32" max="64" value="${s.chars_per_line || 48}" />
        </div>
        <div class="form-group">
          <label>Font Size</label>
          <select id="prn-font">
            <option value="small" ${s.font_size==='small'?'selected':''}>Small</option>
            <option value="normal" ${s.font_size==='normal'?'selected':''}>Normal</option>
            <option value="large" ${s.font_size==='large'?'selected':''}>Large</option>
          </select>
        </div>
        <div class="form-group">
          <label>Auto Paper Cut</label>
          <select id="prn-cut">
            <option value="true" ${s.auto_cut!==false?'selected':''}>Yes</option>
            <option value="false" ${s.auto_cut===false?'selected':''}>No</option>
          </select>
        </div>
        <div class="form-group">
          <label>USB Vendor ID (hex, optional)</label>
          <input type="text" id="prn-vid" placeholder="04b8" value="${s.usb_vendor_id || ''}" />
        </div>
        <div class="form-group">
          <label>USB Product ID (hex, optional)</label>
          <input type="text" id="prn-pid" placeholder="0202" value="${s.usb_product_id || ''}" />
        </div>
      </div>
      <div style="display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;">
        <button onclick="savePrinterSettings()" class="btn-admin-action">💾 Save Settings</button>
        <button onclick="testThermalPrinter('english')" class="btn-admin-action" style="background:#059669;">🧪 Test Print (EN)</button>
        <button onclick="testThermalPrinter('tamil')" class="btn-admin-action" style="background:#166534;font-size:13px;">🧪 தமிழ் Test Print</button>
        <button onclick="refreshPrinterList()" style="padding:10px 18px;border:1.5px solid #7C3AED;background:#f3f0ff;color:#7C3AED;border-radius:10px;font-weight:700;cursor:pointer;">🔄 Refresh Printers</button>
        <button onclick="previewSampleReceipt('en')" style="padding:10px 18px;border:1.5px solid #ddd;background:#fff;border-radius:10px;font-weight:700;cursor:pointer;">👁 Sample Preview</button>
        <button onclick="previewSampleReceipt('ta')" style="padding:10px 18px;border:1.5px solid #166534;background:#ecfdf5;color:#166534;border-radius:10px;font-weight:700;cursor:pointer;">தமிழ் Sample</button>
        <button onclick="window.open('/debug/tamil-raster', '_blank')" style="padding:10px 18px;border:1.5px solid #7c3aed;background:#f5f3ff;color:#7c3aed;border-radius:10px;font-weight:700;cursor:pointer;">🖼️ தமிழ் Raster Preview</button>
        <button onclick="detectQZPrinters()" style="padding:10px 18px;border:1.5px solid #059669;background:#ecfdf5;color:#059669;border-radius:10px;font-weight:700;cursor:pointer;">🔌 QZ Printers</button>
      </div>
      <pre id="prn-debug-log" style="margin-top:14px;padding:10px;background:#f8f8f8;border-radius:8px;font-size:11px;max-height:120px;overflow:auto;display:none;"></pre>
    </div>
    <div class="admin-card" style="margin-top:20px;border-top:3px solid #166534;">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
        <div>
          <h3 style="color:#166534;margin:0 0 4px 0;">🔬 Printer Diagnostics — Tamil Hardware Test</h3>
          <p style="color:#64748b;font-size:12px;margin:0;">
            Validate raw ESC/POS bitImageRaster (576px / 80mm) with HarfBuzz shaping on client physical thermal printer.
          </p>
        </div>
        <div id="hw-val-badge-container">
          <span id="hw-val-badge" style="background:#e2e8f0;color:#475569;padding:6px 12px;border-radius:20px;font-size:12px;font-weight:700;">AWAITING TEST</span>
        </div>
      </div>

      <div style="display:flex;gap:12px;margin-top:16px;flex-wrap:wrap;">
        <button onclick="runTamilHardwareTest('raw-raster')" class="btn-admin-action" style="background:#0f766e;font-size:13px;">
          ⚡ TEST 1: Raw Raster Test (தமிழ் PRINTER TEST)
        </button>
        <button onclick="runTamilHardwareTest('full-bill')" class="btn-admin-action" style="background:#166534;font-size:13px;">
          🧾 TEST 2: Full Tamil Bill Test
        </button>
        <button onclick="openHardwareConfirmationModal()" style="padding:10px 18px;border:1.5px solid #166534;background:#f0fdf4;color:#166534;border-radius:10px;font-weight:700;cursor:pointer;">
          📋 Test 4: Physical Paper Validation Checklist
        </button>
      </div>

      <!-- Live Metadata Display -->
      <div id="hw-test-meta-card" style="display:none;margin-top:16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px;">
        <h4 style="margin:0 0 10px 0;font-size:13px;color:#1e293b;display:flex;align-items:center;gap:8px;">
          <span>📊 Live Hardware Test Telemetry</span>
          <span id="hw-meta-soft-status" style="background:#10b981;color:#fff;font-size:10px;padding:2px 8px;border-radius:12px;font-weight:700;">SOFTWARE VALIDATION: PASS</span>
        </h4>
        <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));gap:10px;font-size:12px;">
          <div><strong>Bitmap Width:</strong> <span id="hw-meta-w">576px</span></div>
          <div><strong>Bitmap Height:</strong> <span id="hw-meta-h">-</span></div>
          <div><strong>ESC/POS Bytes:</strong> <span id="hw-meta-bytes">-</span></div>
          <div><strong>Transport:</strong> <span id="hw-meta-transport">-</span></div>
          <div><strong>Paper Width:</strong> <span id="hw-meta-paper">80mm</span></div>
          <div><strong>Timestamp:</strong> <span id="hw-meta-time">-</span></div>
        </div>
        <div style="margin-top:10px;font-size:11px;color:#64748b;display:flex;gap:15px;align-items:center;flex-wrap:wrap;">
          <span>Artifacts:</span>
          <a href="/debug/client-hardware-test.png" target="_blank" style="color:#0f766e;font-weight:600;">🖼️ View Bitmap (PNG)</a>
          <a href="/debug/client-hardware-test.bin" download="client_tamil_hardware_test.bin" style="color:#7c3aed;font-weight:600;">💾 Download Raw ESC/POS (.bin)</a>
        </div>
        <div id="hw-meta-note" style="margin-top:8px;font-size:11px;color:#475569;background:#f1f5f9;padding:6px 10px;border-radius:6px;"></div>
      </div>
    </div>`;
    _printerSettings = s;
    loadHardwareTestStatus();
}

async function loadHardwareTestStatus() {
    try {
        const r = await fetch('/api/printer/hardware-test/status');
        const d = await r.json();
        if (d.success && d.validation && d.validation.physical_printer_validation === 'PASS') {
            const badge = document.getElementById('hw-val-badge');
            if (badge) {
                badge.style.background = '#166534';
                badge.style.color = '#ffffff';
                badge.textContent = 'PHYSICAL PRINTER VALIDATION: PASS';
            }
        }
    } catch (e) {}
}

async function runTamilHardwareTest(testType = 'raw-raster') {
    const endpoint = testType === 'raw-raster'
        ? '/api/printer/hardware-test/raw-raster'
        : '/api/printer/hardware-test/full-bill';
    const label = testType === 'raw-raster' ? 'Raw Raster Test' : 'Full Tamil Bill';
    showToast(`⏳ Running ${label}...`);

    try {
        const r = await fetch(endpoint, { method: 'POST' });
        const d = await r.json();
        if (d.success) {
            const card = document.getElementById('hw-test-meta-card');
            if (card) {
                card.style.display = 'block';
                document.getElementById('hw-meta-w').textContent = `${d.bitmap_width}px`;
                document.getElementById('hw-meta-h').textContent = `${d.bitmap_height}px`;
                document.getElementById('hw-meta-bytes').textContent = `${d.escpos_bytes_length} bytes`;
                document.getElementById('hw-meta-transport').textContent = d.printer_transport || '-';
                document.getElementById('hw-meta-paper').textContent = `${d.paper_width_mm}mm`;
                document.getElementById('hw-meta-time').textContent = new Date(d.timestamp).toLocaleTimeString();
                document.getElementById('hw-meta-note').textContent = `Status: ${d.print_status} — ${d.print_message}`;
            }

            const badge = document.getElementById('hw-val-badge');
            if (badge && badge.textContent !== 'PHYSICAL PRINTER VALIDATION: PASS') {
                badge.style.background = '#0284c7';
                badge.style.color = '#ffffff';
                badge.textContent = 'SOFTWARE VALIDATION: PASS';
            }

            showToast(`✅ ${label} generated! Software validation: PASS`);
        } else {
            showToast(`⚠️ Test error: ${d.message || 'Failed'}`);
        }
    } catch (e) {
        showToast(`❌ Error: ${e.message}`);
    }
}

function openHardwareConfirmationModal() {
    const modalId = 'modal-hw-val-confirm';
    let modal = document.getElementById(modalId);
    if (modal) modal.remove();

    modal = document.createElement('div');
    modal.id = modalId;
    modal.style.position = 'fixed';
    modal.style.top = '0';
    modal.style.left = '0';
    modal.style.width = '100%';
    modal.style.height = '100%';
    modal.style.background = 'rgba(15, 23, 42, 0.6)';
    modal.style.zIndex = '999999';
    modal.style.display = 'flex';
    modal.style.alignItems = 'center';
    modal.style.justifyContent = 'center';
    modal.style.backdropFilter = 'blur(4px)';

    modal.innerHTML = `
      <div style="background:#ffffff;border-radius:16px;width:90%;max-width:620px;max-height:90vh;overflow-y:auto;padding:24px;box-shadow:0 25px 50px -12px rgba(0,0,0,0.25);">
        <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #e2e8f0;padding-bottom:12px;margin-bottom:16px;">
          <h3 style="margin:0;color:#166534;font-size:18px;">📋 Physical Paper Validation Checklist</h3>
          <button onclick="document.getElementById('${modalId}').remove()" style="background:none;border:none;font-size:20px;cursor:pointer;color:#64748b;">✕</button>
        </div>
        <p style="font-size:13px;color:#475569;margin-bottom:16px;">
          Inspect the printed physical paper receipt from your thermal printer. All 12 items must be verified before marking physical validation as PASS:
        </p>
        <form id="hw-val-form" onsubmit="submitHardwareConfirmation(event)">
          <div style="display:flex;flex-direction:column;gap:10px;font-size:13px;">
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="tamil_glyphs_readable" required /> <span>Tamil glyphs readable (No jumbled or corrupted text)</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="tamil_vowels_consonants_correct" required /> <span>Tamil vowel/consonant combinations correct (Proper pre-base vowels ெ, ே, ை)</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="no_missing_glyphs" required /> <span>No missing glyphs or square boxes</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="no_clipping_left" required /> <span>No clipping on left margin</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="no_clipping_right" required /> <span>No clipping on right margin (Full 80mm width utilized)</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="product_names_wrap" required /> <span>Product names wrap correctly without touching Qty</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="qty_column_aligned" required /> <span>Qty column properly right-aligned</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="amount_column_aligned" required /> <span>Amount column properly right-aligned</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="currency_and_numbers_correct" required /> <span>₹ symbol and numbers correct</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="total_correct" required /> <span>Total row prominent with double rules</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="payment_section_correct" required /> <span>Payment section (Mode, Paid, Due, Loyalty) aligned</span></label>
            <label style="display:flex;align-items:center;gap:10px;"><input type="checkbox" name="footer_complete" required /> <span>Footer complete with greetings and paper cut</span></label>
          </div>
          <div style="margin-top:16px;display:grid;grid-template-columns:1fr 1fr;gap:12px;">
            <div>
              <label style="font-size:12px;font-weight:600;color:#334155;">Operator Name</label>
              <input type="text" id="hw-op-name" placeholder="Your Name" style="width:100%;padding:8px 12px;border:1px solid #cbd5e1;border-radius:8px;box-sizing:border-box;" required />
            </div>
            <div>
              <label style="font-size:12px;font-weight:600;color:#334155;">Notes / Observations</label>
              <input type="text" id="hw-op-notes" placeholder="e.g. Crisp dark output on 80mm" style="width:100%;padding:8px 12px;border:1px solid #cbd5e1;border-radius:8px;box-sizing:border-box;" />
            </div>
          </div>
          <div style="margin-top:20px;display:flex;justify-content:flex-end;gap:12px;">
            <button type="button" onclick="document.getElementById('${modalId}').remove()" style="padding:10px 18px;border:1px solid #cbd5e1;background:#f8fafc;border-radius:8px;cursor:pointer;font-weight:600;">Cancel</button>
            <button type="submit" style="padding:10px 20px;background:#166534;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:700;">✅ Confirm Physical Print Validation</button>
          </div>
        </form>
      </div>
    `;

    document.body.appendChild(modal);
}

async function submitHardwareConfirmation(event) {
    event.preventDefault();
    const form = document.getElementById('hw-val-form');
    if (!form) return;

    const checklist = {
        tamil_glyphs_readable: form.tamil_glyphs_readable.checked,
        tamil_vowels_consonants_correct: form.tamil_vowels_consonants_correct.checked,
        no_missing_glyphs: form.no_missing_glyphs.checked,
        no_clipping_left: form.no_clipping_left.checked,
        no_clipping_right: form.no_clipping_right.checked,
        product_names_wrap: form.product_names_wrap.checked,
        qty_column_aligned: form.qty_column_aligned.checked,
        amount_column_aligned: form.amount_column_aligned.checked,
        currency_and_numbers_correct: form.currency_and_numbers_correct.checked,
        total_correct: form.total_correct.checked,
        payment_section_correct: form.payment_section_correct.checked,
        footer_complete: form.footer_complete.checked,
    };

    const operator_name = document.getElementById('hw-op-name')?.value || 'Operator';
    const notes = document.getElementById('hw-op-notes')?.value || '';

    try {
        const r = await fetch('/api/printer/hardware-test/confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ checklist, operator_name, notes }),
        });
        const d = await r.json();
        if (d.success) {
            const modal = document.getElementById('modal-hw-val-confirm');
            if (modal) modal.remove();

            const badge = document.getElementById('hw-val-badge');
            if (badge) {
                badge.style.background = '#166534';
                badge.style.color = '#ffffff';
                badge.textContent = 'PHYSICAL PRINTER VALIDATION: PASS';
            }
            showToast('🎉 Physical printer validation recorded: PASS');
        } else {
            showToast(`⚠️ ${d.message || 'Validation failed'}`);
        }
    } catch (e) {
        showToast(`❌ Confirmation error: ${e.message}`);
    }
}

async function savePrinterSettings() {
    const settings = {
        mode: document.getElementById('prn-mode')?.value || 'escpos',
        printer_name: document.getElementById('prn-name')?.value || '',
        paper_width_mm: parseInt(document.getElementById('prn-width')?.value || '80', 10),
        chars_per_line: parseInt(document.getElementById('prn-chars')?.value || '48', 10),
        font_size: document.getElementById('prn-font')?.value || 'normal',
        auto_cut: document.getElementById('prn-cut')?.value === 'true',
        usb_vendor_id: document.getElementById('prn-vid')?.value?.trim() || '',
        usb_product_id: document.getElementById('prn-pid')?.value?.trim() || '',
        qz_printer_name: document.getElementById('prn-qz-name')?.value?.trim() || '',
    };
    if (settings.paper_width_mm === 58) settings.chars_per_line = settings.chars_per_line || 32;
    try {
        const r = await fetch('/api/printer/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ settings }),
        });
        const d = await r.json();
        if (d.success) {
            _printerSettings = d.settings;
            showToast('Printer settings saved');
        } else showToast('Save failed');
    } catch (e) {
        showToast('Save error: ' + e.message);
    }
}

async function testThermalPrinter(language = 'english') {
    await savePrinterSettings();
    const lang = (language === 'tamil' || language === 'ta') ? 'tamil' : 'english';
    const label = lang === 'tamil' ? 'தமிழ்' : 'English';
    try {
        const r = await fetch('/api/printer/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ language: lang }),
        });
        const d = await r.json();
        const log = document.getElementById('prn-debug-log');
        if (log) {
            log.style.display = 'block';
            log.textContent = `[${label}] ` + (d.success ? 'OK: ' : 'FAIL: ') + (d.message || '') + '\n' + (d.preview || '').slice(0, 600);
        }
        showToast(d.success ? '🖨️ ' + label + ' test print sent' : '⚠️ ' + (d.message || 'Test failed'));
    } catch (e) {
        showToast('Test error: ' + e.message);
    }
}

async function refreshPrinterList() {
    await renderPrinterAdmin();
    showToast('Printer list refreshed');
}

async function detectQZPrinters() {
    if (!window.QZTrayPrint) {
        showToast('⚠️ QZ Tray script not loaded');
        return;
    }
    try {
        const list = await window.QZTrayPrint.listPrinters();
        const log = document.getElementById('prn-debug-log');
        if (log) {
            log.style.display = 'block';
            log.textContent = 'QZ printers:\n' + (list || []).join('\n');
        }
        showToast(list?.length ? `QZ: ${list.length} printer(s)` : 'QZ: no printers found');
    } catch (e) {
        showToast('QZ Tray not running — install from qz.io and start the tray app');
    }
}

async function previewSampleReceipt(language = 'en') {
    const lang = (language === 'ta' || language === 'tamil') ? 'ta' : 'en';
    const btype = window.currentBillType || 'normal';
    const billId = window.currentBill?.id || 'sample';
    if (billId && billId !== 'sample') {
        const previewUrl = `/preview-thermal/${encodeURIComponent(billId)}?language=${lang}&bill_type=${btype}&preview=1`;
        showReceiptPreviewModal(previewUrl, lang);
    } else {
        const previewUrl = `/preview-thermal/sample?language=${lang}&bill_type=${btype}&preview=1`;
        showReceiptPreviewModal(previewUrl, lang);
    }
}

function openSimpleThermalBill(billId, paidAmount) {
    return directThermalPrint(billId, paidAmount);
}
window.openSimpleThermalBill = openSimpleThermalBill;
window.directThermalPrint = directThermalPrint;

document.addEventListener('DOMContentLoaded', () => { loadPrinterSettings(); });
