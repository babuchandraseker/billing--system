/*
 * Behavioural harness for POS product search ranking in frontend/script.js.
 *
 *   node tests/js/search_rank_harness.js [products.json]
 *
 * The real search + render functions are cut out of script.js and run in a
 * sandbox with a tiny fake DOM. Fixture products copy the search fields of the
 * shop's real products (P14 Finger Millet, P220 Amaranth Seeds, ...) with S.NOs
 * chosen so that plain S.NO order would put the wrong product first.
 * If a products.json (the /products API payload) is given, the same queries are
 * also run on that real data under "real".
 * Prints one JSON object for tests/test_production_audit.py.
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

function constSource(name) {
    const start = SRC.indexOf(`const ${name} = {`);
    if (start < 0) throw new Error(`const ${name} not found in script.js`);
    return SRC.slice(start, SRC.indexOf('\n};\n', start) + 3);
}

const FUNCTIONS = [
    'buildDisplayName', '_buildCorpus', 'matchesSearch', '_normSearch', '_searchRank',
    '_sortProductList', 'renderProducts', '_buildProductListHeader', 'getPriceModeLabel',
    '_buildProductCard', '_buildPackageCard', '_h', '_markCartRows', '_attachLazySentinel',
    '_updateLazyCounter', '_removeLoadingIndicator',
];

class El {
    constructor(id) { this.id = id || ''; this.value = ''; this.innerHTML = ''; this.textContent = ''; this.style = {}; }
    appendChild(c) { if (c.id) DOC.els[c.id] = c; return c; }
    insertBefore(c) { return this.appendChild(c); }
    remove() { delete DOC.els[this.id]; }
}
const DOC = {
    els: {},
    getElementById(id) { return this.els[id] || null; },
    createElement() { return new El(); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
};

// Search fields copied from the shop's product data.
const FIXTURE = [
    { id: 'p220', serial_no: 53, name_english: 'Amaranth Seeds', name_tamil: 'அமர்நாத்', name_tanglish: 'amarnaath',
      category: 'Miscellaneous / Internal', search_tags: 'அமர்நாத் amarnaath amarnaath',
      search_terms: ['amaranth', 'seeds', '(rajgira)', 'அமர்நாத்', 'amarnaath'] },
    { id: 'p14', serial_no: 102, name_english: 'Finger Millet', name_tamil: 'கேழ்வரகு', name_tanglish: 'keezhvaraku',
      category: 'Millets', search_tags: 'கேழ்வரகு keezhvaraku keezhvaraku',
      search_terms: ['finger', 'millet', '(ragi)', 'கேழ்வரகு', 'keezhvaraku'] },
    { id: 'p520', serial_no: 1, name_english: 'Test Code Neighbour', name_tamil: 'சோதனை', name_tanglish: 'sothanai',
      category: 'Misc', search_tags: '', search_terms: [] },
    { id: 'p52', serial_no: 60, name_english: 'Black Gram (Urad Dal)', name_tamil: 'கருப்பு உளுந்து', name_tanglish: 'karuppu ulunthu',
      category: 'Pulses', search_tags: 'கருப்பு உளுந்து karuppu ulunthu',
      search_terms: ['black', 'gram', '(urad', 'dal)', 'கருப்பு', 'உளுந்து', 'karuppu', 'ulunthu'] },
    { id: 'p9', serial_no: 9, name_english: 'Red Millet', name_tamil: 'சிவப்பு தினை', name_tanglish: 'sivappu thinai',
      category: 'Millets', search_tags: 'சிவப்பு தினை sivappu thinai', search_terms: ['red', 'millet', 'சிவப்பு', 'தினை', 'sivappu', 'thinai'] },
    { id: 'p2', serial_no: 2, name_english: 'Red Foxtail Millet', name_tamil: 'செந்தினை', name_tanglish: 'chenthinai',
      category: 'Millets', search_tags: 'செந்தினை chenthinai', search_terms: ['red', 'foxtail', 'millet', 'செந்தினை', 'chenthinai'] },
    { id: 'p1', serial_no: 30, name_english: 'Foxtail Millet (Thinai)', name_tamil: 'தினை', name_tanglish: 'thinai',
      category: 'Millets', search_tags: 'தினை thinai', search_terms: ['foxtail', 'millet', '(thinai)', 'தினை', 'thinai'] },
    { id: 'p5', serial_no: 5, name_english: 'Native Pearl Millet', name_tamil: 'நாட்டுகம்பு', name_tanglish: 'naattukampu',
      category: 'Millets', search_tags: 'நாட்டுகம்பு naattukampu', search_terms: ['native', 'pearl', 'millet', 'நாட்டுகம்பு', 'naattukampu'] },
    { id: 'p15', serial_no: 15, name_english: 'Pearl Millet', name_tamil: 'கம்பு', name_tanglish: 'kampu',
      category: 'Millets', search_tags: 'கம்பு kampu', search_terms: ['pearl', 'millet', 'கம்பு', 'kampu'] },
];

const QUERIES = ['ragi', 'Ragi', 'P52', 'p52', 'finger millet', 'Finger Millet', 'கேழ்வரகு', 'keezhvaraku',
    'thinai', 'தினை', 'pearl millet', 'kampu', 'kambu', 'millet', 'urad', 'amarnaath', 'rajgira', 'zzzz'];

function makeSandbox(products) {
    const sb = { document: DOC, console, IntersectionObserver: class { observe() {} disconnect() {} } };
    vm.createContext(sb);
    vm.runInContext([
        constSource('SEARCH_SYNONYMS'),
        ...FUNCTIONS.map(fnSource),
        "var products = [], packages = [], cart = [], activeCat = 'All', pricingMode = 's_rate';",
        'var PAGE_SIZE = 50, _lazyList = [], _lazyRendered = 0, _lazyLoading = false, _lazyObserver = null, _kbIndex = -1;',
    ].join('\n'), sb);
    sb.products = products.map(p => {
        const c = Object.assign({}, p);
        c.shortcode = (c.id || '').toUpperCase();          // same as fetchProducts()
        c.displayName = vm.runInContext('buildDisplayName', sb)(c);
        return c;
    });
    return sb;
}

function run(products) {
    const sb = makeSandbox(products);
    const out = {};
    for (const q of QUERIES) {
        // 1. ranked order exactly as renderProducts() builds it
        DOC.els = { productSearch: new El('productSearch'), 'products-grid': new El('products-grid'),
                    'product-count-badge': new El('product-count-badge') };
        DOC.els.productSearch.value = q;
        vm.runInContext('renderProducts()', sb);
        const html = DOC.els['products-grid'].innerHTML;
        const rendered = [...html.matchAll(/class="pcard[^"]*" id="pcard-([^"]+)"/g)].map(m => m[1]);
        const order = vm.runInContext('_lazyList', sb).map(p => p.id);
        // 2. membership is still decided by the unchanged matchesSearch()
        const matched = sb.products.filter(p => vm.runInContext('matchesSearch', sb)(p, q)).map(p => p.id);
        const ranks = Object.fromEntries(sb.products.filter(p => matched.includes(p.id))
            .map(p => [p.id, vm.runInContext('_searchRank', sb)(p, q)]));
        out[q] = { order, first_rendered_row: rendered[0] || null, matched, ranks };
    }
    return out;
}

const result = {};
try {
    result.fixture = run(FIXTURE);
    if (process.argv[2]) {
        const real = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
        result.real = run(real.products || real);
    }
} catch (e) {
    result.harness_error = String(e && e.stack || e);
}
console.log(JSON.stringify(result));
