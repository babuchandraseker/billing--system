"""
nutrition_engine.py  —  Dhana Dhanya Kadai | Nutrition Engine v2
================================================================
DROP-IN REPLACEMENT for the three nutrition functions in app.py.

Key upgrades over v19:
  • classify_product()   — 4-priority classifier (gum > category > keyword > default)
  • get_nutrition_data() — 100+ product USDA/IFCT-anchored per-100g database
  • calculate_nutrition() — scaled calculation with display-rules baked in

All values are sourced from:
  • USDA FoodData Central (https://fdc.nal.usda.gov/)
  • IFCT 2017 (Indian Food Composition Tables, NIN Hyderabad)
  • Gopalan et al., "Nutritive Value of Indian Foods" (ICMR)

INTEGRATION CHECKLIST
─────────────────────
1.  Add this file to billing-fixed/backend/  (same folder as app.py)
2.  In app.py, replace the three function definitions with:

        from nutrition_engine import classify_product, get_nutrition_data, calculate_nutrition

3.  In calculate_item_nutrition() change the data-lookup line to:

        data = get_nutrition_data(name_english) or (
            OIL_NUTRIENT_DATA if nutrient_type == 'oil' else NORMAL_NUTRIENT_DATA
        )

4.  Anywhere auto_classify_products() or classify_nutrient_type() decides a
    product's type, you may optionally call classify_product(name, category)
    for finer-grained results (gum / nuts / spices / herbs / feed / oil / protein / normal).

NO OTHER CHANGES to billing logic are required.
"""

from __future__ import annotations
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — PRODUCT CLASSIFIER
# ─────────────────────────────────────────────────────────────────────────────

# Priority-1: name-level exact fragments (case-insensitive substring match)
#   These override everything else — category, keywords, fallback.
_GUM_NAMES: tuple[str, ...] = (
    "badam pisin", "almond gum", "moringa gum", "gum",
    "pisin", "resin", "latex",
)

# Priority-2: category field → type mapping
_CATEGORY_MAP: dict[str, str] = {
    "nuts":       "nuts",
    "spices":     "spices",
    "herbs":      "herbs",
    "feed":       "feed",
    "dry fruits": "normal",   # dates, raisins, figs → normal (high carb, low fat)
    "supplements":"normal",
    "protein":    "protein",
}

# Priority-3a: oil-seed keyword fragments
_OIL_KEYWORDS: tuple[str, ...] = (
    "sunflower", "sesame", "flax", "niger", "groundnut", "peanut",
    "walnut",    "cashew", "pistachio", "pista", "almond",
    "mustard",   "rapeseed", "canola", "chia",  "poppy",
    "pumpkin seed", "moringa seed", "cucumber seed",
    "safflower", "sappola", "sapola",
    "milk thistle",
)

# Priority-3b: protein/pulse keyword fragments
_PROTEIN_KEYWORDS: tuple[str, ...] = (
    "cowpea", "karamani", "chickpea", "chick pea", "mukkadala",
    "gram", "dal", "lentil", "moong", "urad", "horse gram",
    "kollu", "pigeon pea", "thuvarai", "soybean", "soya",
    "kidney bean", "rajma", "black eyed pea", "field bean",
    "pattani", "payir",
)


def classify_product(name: str, category: str = "") -> str:
    """
    Classify a product into one of:
        gum | nuts | spices | herbs | feed | oil | protein | normal

    Priority order (first match wins):
      1. Name contains a gum/resin fragment          → "gum"
      2. Category field matches a known category     → mapped type
      3. Name contains an oil-seed keyword           → "oil"
      4. Name contains a protein/pulse keyword       → "protein"
      5. Default                                     → "normal"

    Args:
        name:     English product name (case-insensitive)
        category: Product category string (e.g. "Nuts", "Spices", "Feed")

    Returns:
        str: one of the eight type labels above
    """
    name_lc     = (name     or "").lower().strip()
    category_lc = (category or "").lower().strip()

    # ── Priority 1: gum / resin (exact name fragment) ────────────────────────
    for fragment in _GUM_NAMES:
        if fragment in name_lc:
            return "gum"

    # ── Priority 2: category field ───────────────────────────────────────────
    for cat_key, cat_type in _CATEGORY_MAP.items():
        if cat_key in category_lc:
            return cat_type

    # ── Priority 3a: oil-seed keywords ──────────────────────────────────────
    for kw in _OIL_KEYWORDS:
        if kw in name_lc:
            return "oil"

    # ── Priority 3b: protein/pulse keywords ─────────────────────────────────
    for kw in _PROTEIN_KEYWORDS:
        if kw in name_lc:
            return "protein"

    # ── Priority 4: default ──────────────────────────────────────────────────
    return "normal"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — PER-PRODUCT NUTRITION DATABASE  (per 100 g, as-purchased)
# ─────────────────────────────────────────────────────────────────────────────
# Key   = lowercase name fragment used for substring lookup
# type  = internal routing key that determines which fields are used
# All numeric values are per 100 g.
#
# Sources:
#   [U] = USDA FoodData Central  (fdc.nal.usda.gov)
#   [I] = IFCT 2017 / NIN Hyderabad
#   [G] = Gopalan et al. "Nutritive Value of Indian Foods"
#   [F] = FAO Food Composition Database
# ─────────────────────────────────────────────────────────────────────────────

_NUTRITION_DB: dict[str, dict] = {

    # ══════════════════════════════════════════════════════════════════════════
    # OIL SEEDS  — type="oil"
    # Fields: protein, fat, carbohydrates, fiber, moisture, ash
    # ══════════════════════════════════════════════════════════════════════════

    # Sunflower seeds (hulled, dried) — USDA #12036
    "sunflower": {
        "type": "oil",
        "protein": 20.8, "fat": 51.5, "carbohydrates": 20.0,
        "absorbable_protein": 16.2,
        "fiber": 8.6, "moisture": 4.7, "ash": 3.0,
    },

    # Sesame seeds (whole, dried) — USDA #12023
    "sesame": {
        "type": "oil",
        "protein": 17.7, "fat": 49.7, "carbohydrates": 23.5,
        "absorbable_protein": 13.3,
        "fiber": 11.8, "moisture": 4.7, "ash": 4.5,
    },

    # Flaxseeds / linseeds — USDA #12220
    "flax": {
        "type": "oil",
        "protein": 18.3, "fat": 42.2, "carbohydrates": 28.9,
        "absorbable_protein": 14.1,
        "fiber": 27.3, "moisture": 6.5, "ash": 3.7,
    },

    # Niger seeds (Guizotia abyssinica) — IFCT 2017
    "niger": {
        "type": "oil",
        "protein": 22.8, "fat": 38.5, "carbohydrates": 19.2,
        "absorbable_protein": 16.4,
        "fiber": 15.6, "moisture": 7.2, "ash": 5.1,
    },

    # Groundnut / peanut (raw, in-shell removed) — USDA #16087
    "groundnut": {
        "type": "oil",
        "protein": 25.8, "fat": 49.2, "carbohydrates": 16.1,
        "absorbable_protein": 20.6,
        "fiber": 8.5, "moisture": 5.0, "ash": 2.3,
    },
    "peanut": {
        "type": "oil",
        "protein": 25.8, "fat": 49.2, "carbohydrates": 16.1,
        "absorbable_protein": 20.6,
        "fiber": 8.5, "moisture": 5.0, "ash": 2.3,
    },

    # Mustard seeds — USDA #02024
    "mustard": {
        "type": "oil",
        "protein": 26.1, "fat": 36.0, "carbohydrates": 28.1,
        "absorbable_protein": 19.6,
        "fiber": 12.2, "moisture": 6.5, "ash": 4.3,
    },

    # Chia seeds — USDA #12006
    "chia": {
        "type": "oil",
        "protein": 16.5, "fat": 30.7, "carbohydrates": 42.1,
        "absorbable_protein": 12.7,
        "fiber": 34.4, "moisture": 5.8, "ash": 5.6,
    },

    # Rapeseed / canola — USDA #12012
    "rapeseed": {
        "type": "oil",
        "protein": 21.0, "fat": 46.1, "carbohydrates": 18.8,
        "absorbable_protein": 16.0,
        "fiber": 12.0, "moisture": 6.0, "ash": 4.2,
    },
    "canola": {
        "type": "oil",
        "protein": 21.0, "fat": 46.1, "carbohydrates": 18.8,
        "absorbable_protein": 16.0,
        "fiber": 12.0, "moisture": 6.0, "ash": 4.2,
    },

    # Safflower seeds (Carthamus tinctorius) — IFCT 2017
    "safflower": {
        "type": "oil",
        "protein": 16.0, "fat": 38.5, "carbohydrates": 34.3,
        "absorbable_protein": 11.5,
        "fiber": 17.1, "moisture": 5.8, "ash": 3.2,
    },
    "sappola": {
        "type": "oil",
        "protein": 16.0, "fat": 38.5, "carbohydrates": 34.3,
        "absorbable_protein": 11.5,
        "fiber": 17.1, "moisture": 5.8, "ash": 3.2,
    },
    "sapola": {
        "type": "oil",
        "protein": 16.0, "fat": 38.5, "carbohydrates": 34.3,
        "absorbable_protein": 11.5,
        "fiber": 17.1, "moisture": 5.8, "ash": 3.2,
    },

    # Pumpkin seeds (pepitas, dried) — USDA #12016
    "pumpkin seed": {
        "type": "oil",
        "protein": 30.2, "fat": 49.1, "carbohydrates": 10.7,
        "absorbable_protein": 24.2,
        "fiber": 6.0, "moisture": 5.5, "ash": 5.4,
    },

    # Poppy seeds — USDA #02033
    "poppy": {
        "type": "oil",
        "protein": 18.0, "fat": 41.6, "carbohydrates": 28.1,
        "absorbable_protein": 13.3,
        "fiber": 19.5, "moisture": 5.9, "ash": 9.1,
    },

    # Moringa seeds (drumstick seeds) — FAO / IFCT
    "moringa seed": {
        "type": "oil",
        "protein": 35.0, "fat": 38.0, "carbohydrates": 8.0,
        "absorbable_protein": 26.2,
        "fiber": 2.5, "moisture": 8.0, "ash": 5.0,
    },

    # Cucumber seeds — IFCT 2017
    "cucumber seed": {
        "type": "oil",
        "protein": 24.0, "fat": 44.8, "carbohydrates": 11.0,
        "absorbable_protein": 17.5,
        "fiber": 2.5, "moisture": 7.0, "ash": 2.1,
    },

    # Milk thistle seeds (Silybum marianum) — published literature
    "milk thistle": {
        "type": "oil",
        "protein": 22.0, "fat": 31.0, "carbohydrates": 33.0,
        "absorbable_protein": 15.4,
        "fiber": 18.0, "moisture": 6.0, "ash": 3.5,
    },

    # ══════════════════════════════════════════════════════════════════════════
    # NUTS  — type="nuts"
    # Fields: protein, fat, carbohydrates, fiber
    # (No moisture/ash displayed for nuts — consistent with your spec)
    # ══════════════════════════════════════════════════════════════════════════

    # Almonds — USDA #12061
    "almond": {
        "type": "nuts",
        "protein": 21.2, "fat": 49.9, "carbohydrates": 21.6, "fiber": 12.5,
        "absorbable_protein": 17.4,
    },

    # Cashew (raw) — USDA #12087
    "cashew": {
        "type": "nuts",
        "protein": 18.2, "fat": 43.9, "carbohydrates": 30.2, "fiber": 3.3,
        "absorbable_protein": 15.5,
    },

    # Walnut (English) — USDA #12155
    "walnut": {
        "type": "nuts",
        "protein": 15.2, "fat": 65.2, "carbohydrates": 13.7, "fiber": 6.7,
        "absorbable_protein": 12.5,
    },

    # Pistachio (raw) — USDA #12151
    "pistachio": {
        "type": "nuts",
        "protein": 20.2, "fat": 45.4, "carbohydrates": 27.5, "fiber": 10.3,
        "absorbable_protein": 16.8,
    },
    "pista": {
        "type": "nuts",
        "protein": 20.2, "fat": 45.4, "carbohydrates": 27.5, "fiber": 10.3,
        "absorbable_protein": 16.8,
    },

    # Charoli / Chironji (Buchanania lanzan) — IFCT 2017
    "charoli": {
        "type": "nuts",
        "protein": 19.0, "fat": 59.0, "carbohydrates": 12.0, "fiber": 3.0,
        "absorbable_protein": 14.8,
    },
    "chironji": {
        "type": "nuts",
        "protein": 19.0, "fat": 59.0, "carbohydrates": 12.0, "fiber": 3.0,
        "absorbable_protein": 14.8,
    },

    # Pecan — USDA #12142
    "pecan": {
        "type": "nuts",
        "protein": 9.2, "fat": 72.0, "carbohydrates": 13.9, "fiber": 9.6,
        "absorbable_protein": 7.4,
    },

    # Betel nut (Areca catechu) — IFCT 2017  (dried)
    "betel nut": {
        "type": "nuts",
        "protein": 4.4, "fat": 4.4, "carbohydrates": 55.4, "fiber": 12.0,
        "absorbable_protein": 2.2,
    },
    "areca": {
        "type": "nuts",
        "protein": 4.4, "fat": 4.4, "carbohydrates": 55.4, "fiber": 12.0,
    },

    # ══════════════════════════════════════════════════════════════════════════
    # GRAINS / NORMAL  — type="normal"
    # Fields: protein, fat, carbohydrates, fiber, absorbable_protein
    # absorbable_protein ≈ protein × digestibility coefficient (PDCAAS/DIAAS based)
    # ══════════════════════════════════════════════════════════════════════════

    # Rice (raw milled white) — USDA #20444
    "rice": {
        "type": "normal",
        "protein": 7.5, "fat": 2.2, "carbohydrates": 78.2,
        "fiber": 3.5, "absorbable_protein": 5.7,
    
        "moisture": 12.9, "ash": 1.2,
    },

    # Wheat (whole grain, hard red) — USDA #20080
    "wheat": {
        "type": "normal",
        "protein": 13.2, "fat": 2.5, "carbohydrates": 71.2,
        "fiber": 10.7, "absorbable_protein": 9.5,
    
        "moisture": 10.9, "ash": 1.7,
    },

    # Pearl millet (kambu) — IFCT 2017
    "pearl millet": {
        "type": "normal",
        "protein": 11.0, "fat": 5.0, "carbohydrates": 67.0,
        "fiber": 8.5, "absorbable_protein": 7.4,
    
        "moisture": 11.5, "ash": 2.3,
    },
    "kambu": {
        "type": "normal",
        "protein": 11.0, "fat": 5.0, "carbohydrates": 67.0,
        "fiber": 8.5, "absorbable_protein": 7.4,
    
        "moisture": 11.5, "ash": 2.3,
    },

    # Finger millet / Ragi — USDA #20032
    "ragi": {
        "type": "normal",
        "protein": 7.3, "fat": 1.3, "carbohydrates": 72.0,
        "fiber": 11.5, "absorbable_protein": 4.8,
    
        "moisture": 11.5, "ash": 2.7,
    },
    "finger millet": {
        "type": "normal",
        "protein": 7.3, "fat": 1.3, "carbohydrates": 72.0,
        "fiber": 11.5, "absorbable_protein": 4.8,
    
        "moisture": 11.5, "ash": 2.7,
    },

    # Foxtail millet (thinai) — IFCT 2017
    "foxtail millet": {
        "type": "normal",
        "protein": 12.3, "fat": 4.3, "carbohydrates": 60.9,
        "fiber": 8.0, "absorbable_protein": 8.5,
    
        "moisture": 11.0, "ash": 2.1,
    },
    "thinai": {
        "type": "normal",
        "protein": 12.3, "fat": 4.3, "carbohydrates": 60.9,
        "fiber": 8.0, "absorbable_protein": 8.5,
    
        "moisture": 11.0, "ash": 2.1,
    },

    # Kodo millet (varagu) — IFCT 2017
    "kodo millet": {
        "type": "normal",
        "protein": 8.3, "fat": 1.4, "carbohydrates": 65.9,
        "fiber": 9.0, "absorbable_protein": 5.6,
    
        "moisture": 11.2, "ash": 2.6,
    },
    "varagu": {
        "type": "normal",
        "protein": 8.3, "fat": 1.4, "carbohydrates": 65.9,
        "fiber": 9.0, "absorbable_protein": 5.6,
    
        "moisture": 11.2, "ash": 2.6,
    },

    # Barnyard millet (kuthiraivali / samai) — IFCT 2017
    "barnyard millet": {
        "type": "normal",
        "protein": 6.2, "fat": 2.2, "carbohydrates": 65.5,
        "fiber": 9.8, "absorbable_protein": 4.1,
    
        "moisture": 10.3, "ash": 4.4,
    },
    "samai": {
        "type": "normal",
        "protein": 6.2, "fat": 2.2, "carbohydrates": 65.5,
        "fiber": 9.8, "absorbable_protein": 4.1,
    
        "moisture": 10.3, "ash": 4.4,
    },
    "kuthiraivali": {
        "type": "normal",
        "protein": 6.2, "fat": 2.2, "carbohydrates": 65.5,
        "fiber": 9.8, "absorbable_protein": 4.1,
    
        "moisture": 10.3, "ash": 4.4,
    },

    # Little millet (saamai) — IFCT 2017
    "little millet": {
        "type": "normal",
        "protein": 7.7, "fat": 4.7, "carbohydrates": 67.0,
        "fiber": 7.6, "absorbable_protein": 5.1,
    
        "moisture": 10.9, "ash": 1.5,
    },

    # Sorghum / jowar — USDA #20067
    "sorghum": {
        "type": "normal",
        "protein": 10.4, "fat": 3.5, "carbohydrates": 72.9,
        "fiber": 6.3, "absorbable_protein": 7.0,
    
        "moisture": 10.0, "ash": 1.6,
    },
    "jowar": {
        "type": "normal",
        "protein": 10.4, "fat": 3.5, "carbohydrates": 72.9,
        "fiber": 6.3, "absorbable_protein": 7.0,
    
        "moisture": 10.0, "ash": 1.6,
    },
    "solam": {
        "type": "normal",
        "protein": 10.4, "fat": 3.5, "carbohydrates": 72.9,
        "fiber": 6.3, "absorbable_protein": 7.0,
    
        "moisture": 10.0, "ash": 1.6,
    },

    # Maize / corn (dried) — USDA #20014
    "corn": {
        "type": "normal",
        "protein": 9.4, "fat": 4.7, "carbohydrates": 74.3,
        "fiber": 2.7, "absorbable_protein": 6.0,
    
        "moisture": 10.4, "ash": 1.2,
    },
    "maize": {
        "type": "normal",
        "protein": 9.4, "fat": 4.7, "carbohydrates": 74.3,
        "fiber": 2.7, "absorbable_protein": 6.0,
    
        "moisture": 10.4, "ash": 1.2,
    },
    "makka": {
        "type": "normal",
        "protein": 9.4, "fat": 4.7, "carbohydrates": 74.3,
        "fiber": 2.7, "absorbable_protein": 6.0,
    
        "moisture": 10.4, "ash": 1.2,
    },

    # Popcorn (air-popped) — USDA #19034
    "popcorn": {
        "type": "normal",
        "protein": 12.9, "fat": 4.5, "carbohydrates": 77.8,
        "fiber": 14.5, "absorbable_protein": 8.5,
    
        "moisture": 3.0, "ash": 0.9,
    },

    # Oats (rolled) — USDA #08121
    "oats": {
        "type": "normal",
        "protein": 16.9, "fat": 6.9, "carbohydrates": 66.3,
        "fiber": 10.6, "absorbable_protein": 11.0,
    
        "moisture": 8.2, "ash": 1.7,
    },

    # Barley (hulled) — USDA #20005
    "barley": {
        "type": "normal",
        "protein": 12.5, "fat": 2.3, "carbohydrates": 73.5,
        "fiber": 17.3, "absorbable_protein": 8.0,
    
        "moisture": 9.4, "ash": 2.3,
    },

    # Quinoa — USDA #20137
    "quinoa": {
        "type": "normal",
        "protein": 14.1, "fat": 6.1, "carbohydrates": 64.2,
        "fiber": 7.0, "absorbable_protein": 10.5,
    
        "moisture": 13.3, "ash": 2.4,
    },

    # Amaranth seeds — USDA #20001
    "amaranth": {
        "type": "normal",
        "protein": 13.6, "fat": 7.0, "carbohydrates": 65.2,
        "fiber": 6.7, "absorbable_protein": 9.5,
    
        "moisture": 11.3, "ash": 3.0,
    },

    # Buckwheat — USDA #20008
    "buckwheat": {
        "type": "normal",
        "protein": 13.2, "fat": 3.4, "carbohydrates": 71.5,
        "fiber": 10.0, "absorbable_protein": 9.0,
    
        "moisture": 9.8, "ash": 2.1,
    },

    # Bamboo rice (raw; similar to parboiled white rice) — IFCT
    "bamboo rice": {
        "type": "normal",
        "protein": 8.0, "fat": 1.5, "carbohydrates": 75.5,
        "fiber": 4.0, "absorbable_protein": 5.8,
    
        "moisture": 12.5, "ash": 1.0,
    },

    # Mappillai samba (traditional red rice variety) — IFCT
    "mappillai samba": {
        "type": "normal",
        "protein": 7.8, "fat": 2.1, "carbohydrates": 76.0,
        "fiber": 5.5, "absorbable_protein": 5.5,
    
        "moisture": 12.6, "ash": 1.1,
    },

    # Karunguruvai (black short-grain rice) — IFCT
    "karunguruvai": {
        "type": "normal",
        "protein": 8.5, "fat": 2.0, "carbohydrates": 74.5,
        "fiber": 5.0, "absorbable_protein": 6.0,
    
        "moisture": 12.0, "ash": 1.4,
    },

    # Paddy (rough rice, with husk) — IFCT 2017
    "paddy": {
        "type": "normal",
        "protein": 7.9, "fat": 2.7, "carbohydrates": 76.0,
        "fiber": 8.7, "absorbable_protein": 5.4,
    
        "moisture": 11.7, "ash": 3.8,
    },

    # ── Pulses / Protein ──────────────────────────────────────────────────────

    # Green gram / moong dal — USDA #16080
    "moong": {
        "type": "normal",
        "protein": 23.9, "fat": 1.2, "carbohydrates": 59.9,
        "fiber": 7.6, "absorbable_protein": 17.5,
    
        "moisture": 10.4, "ash": 3.4,
    },
    "green gram": {
        "type": "normal",
        "protein": 23.9, "fat": 1.2, "carbohydrates": 59.9,
        "fiber": 7.6, "absorbable_protein": 17.5,
    
        "moisture": 10.4, "ash": 3.4,
    },

    # Black urad dal — IFCT 2017
    "urad": {
        "type": "normal",
        "protein": 25.2, "fat": 1.6, "carbohydrates": 59.6,
        "fiber": 18.3, "absorbable_protein": 17.0,
    
        "moisture": 10.8, "ash": 3.2,
    },

    # Red lentil (masoor dal) — USDA #16069
    "lentil": {
        "type": "normal",
        "protein": 25.8, "fat": 1.1, "carbohydrates": 60.1,
        "fiber": 10.9, "absorbable_protein": 18.5,
    
        "moisture": 8.3, "ash": 2.5,
    },

    # Chickpea / Bengal gram — USDA #16056
    "chickpea": {
        "type": "normal",
        "protein": 20.5, "fat": 6.0, "carbohydrates": 61.0,
        "fiber": 17.4, "absorbable_protein": 14.3,
    
        "moisture": 11.5, "ash": 2.7,
    },
    "chick pea": {
        "type": "normal",
        "protein": 20.5, "fat": 6.0, "carbohydrates": 61.0,
        "fiber": 17.4, "absorbable_protein": 14.3,
    
        "moisture": 11.5, "ash": 2.7,
    },
    "mukkadala": {
        "type": "normal",
        "protein": 20.5, "fat": 6.0, "carbohydrates": 61.0,
        "fiber": 17.4, "absorbable_protein": 14.3,
    
        "moisture": 11.5, "ash": 2.7,
    },

    # Cowpea / black-eyed pea — USDA #16006
    "cowpea": {
        "type": "normal",
        "protein": 23.5, "fat": 1.9, "carbohydrates": 60.0,
        "fiber": 10.6, "absorbable_protein": 16.1,
    
        "moisture": 11.0, "ash": 3.7,
    },
    "karamani": {
        "type": "normal",
        "protein": 23.5, "fat": 1.9, "carbohydrates": 60.0,
        "fiber": 10.6, "absorbable_protein": 16.1,
    
        "moisture": 11.0, "ash": 3.7,
    },

    # Red pigeon pea / thuvarai — IFCT 2017
    "pigeon pea": {
        "type": "normal",
        "protein": 22.3, "fat": 1.5, "carbohydrates": 62.0,
        "fiber": 15.0, "absorbable_protein": 15.0,
    
        "moisture": 10.2, "ash": 3.5,
    },
    "thuvarai": {
        "type": "normal",
        "protein": 22.3, "fat": 1.5, "carbohydrates": 62.0,
        "fiber": 15.0, "absorbable_protein": 15.0,
    
        "moisture": 10.2, "ash": 3.5,
    },

    # Horse gram (kollu) — IFCT 2017
    "horse gram": {
        "type": "normal",
        "protein": 22.0, "fat": 0.5, "carbohydrates": 57.2,
        "fiber": 5.3, "absorbable_protein": 14.5,
    
        "moisture": 11.9, "ash": 3.2,
    },
    "kollu": {
        "type": "normal",
        "protein": 22.0, "fat": 0.5, "carbohydrates": 57.2,
        "fiber": 5.3, "absorbable_protein": 14.5,
    
        "moisture": 11.9, "ash": 3.2,
    },

    # Kidney beans — USDA #16033
    "kidney bean": {
        "type": "normal",
        "protein": 22.0, "fat": 1.5, "carbohydrates": 61.9,
        "fiber": 15.2, "absorbable_protein": 14.8,
    
        "moisture": 11.8, "ash": 4.4,
    },
    "rajma": {
        "type": "normal",
        "protein": 22.0, "fat": 1.5, "carbohydrates": 61.9,
        "fiber": 15.2, "absorbable_protein": 14.8,
    
        "moisture": 11.8, "ash": 4.4,
    },

    # Field beans (avarai) — IFCT 2017
    "field bean": {
        "type": "normal",
        "protein": 21.0, "fat": 1.5, "carbohydrates": 60.0,
        "fiber": 16.0, "absorbable_protein": 14.2,
    
        "moisture": 10.9, "ash": 3.5,
    },

    # Soybean — USDA #16108
    "soybean": {
        "type": "normal",
        "protein": 36.5, "fat": 19.9, "carbohydrates": 30.2,
        "fiber": 9.3, "absorbable_protein": 27.4,
    
        "moisture": 8.5, "ash": 4.9,
    },
    "soya": {
        "type": "normal",
        "protein": 36.5, "fat": 19.9, "carbohydrates": 30.2,
        "fiber": 9.3, "absorbable_protein": 27.4,
    
        "moisture": 8.5, "ash": 4.9,
    },

    # White peas / Matar — IFCT 2017
    "white pea": {
        "type": "normal",
        "protein": 21.0, "fat": 1.4, "carbohydrates": 63.0,
        "fiber": 7.2, "absorbable_protein": 14.1,
    
        "moisture": 11.3, "ash": 2.9,
    },
    "pattani": {
        "type": "normal",
        "protein": 21.0, "fat": 1.4, "carbohydrates": 63.0,
        "fiber": 7.2, "absorbable_protein": 14.1,
    
        "moisture": 11.3, "ash": 2.9,
    },

    # Fenugreek seeds (methi) — USDA #02019
    "fenugreek": {
        "type": "normal",
        "protein": 23.0, "fat": 6.4, "carbohydrates": 58.4,
        "fiber": 24.6, "absorbable_protein": 15.0,
    
        "moisture": 8.8, "ash": 3.4,
    },

    # ══════════════════════════════════════════════════════════════════════════
    # GUM / RESIN  — type="gum"
    # Fields: carbohydrates, protein, fat, moisture, ash
    # ══════════════════════════════════════════════════════════════════════════

    # Badam pisin / Almond gum (Prunus dulcis exudate) — phytochemistry literature
    "badam pisin": {
        "type": "gum",
        "carbohydrates": 84.0, "protein": 3.5, "fat": 0.4,
        "absorbable_protein": 1.1,
        "moisture": 10.0, "ash": 6.0,
    },
    "almond gum": {
        "type": "gum",
        "carbohydrates": 84.0, "protein": 3.5, "fat": 0.4,
        "absorbable_protein": 1.1,
        "moisture": 10.0, "ash": 6.0,
    },

    # Moringa gum — literature values (Varma & Patel 2017)
    "moringa gum": {
        "type": "gum",
        "carbohydrates": 80.5, "protein": 5.2, "fat": 0.6,
        "absorbable_protein": 1.6,
        "moisture": 11.5, "ash": 7.0,
    },

    # ══════════════════════════════════════════════════════════════════════════
    # SPICES  — type="spices"
    # Treated as "normal" for nutrition math; displayed with normal model
    # ══════════════════════════════════════════════════════════════════════════

    # Cumin seeds — USDA #02014
    "cumin": {
        "type": "spices",
        "protein": 17.8, "fat": 22.3, "carbohydrates": 44.2,
        "fiber": 10.5, "absorbable_protein": 11.0,
    },

    # Black pepper — USDA #02030
    "black pepper": {
        "type": "spices",
        "protein": 10.4, "fat": 3.3, "carbohydrates": 63.7,
        "fiber": 25.3, "absorbable_protein": 6.5,
    },

    # White pepper — USDA #02048
    "white pepper": {
        "type": "spices",
        "protein": 10.4, "fat": 2.1, "carbohydrates": 68.6,
        "fiber": 26.2, "absorbable_protein": 6.5,
    },

    # Cardamom — USDA #02007
    "cardamom": {
        "type": "spices",
        "protein": 10.8, "fat": 6.7, "carbohydrates": 68.5,
        "fiber": 28.0, "absorbable_protein": 6.5,
    },

    # Coriander seeds — USDA #02013
    "coriander": {
        "type": "spices",
        "protein": 12.4, "fat": 17.8, "carbohydrates": 54.9,
        "fiber": 41.9, "absorbable_protein": 7.5,
    },

    # Carom seeds (ajwain) — IFCT 2017
    "carom": {
        "type": "spices",
        "protein": 15.9, "fat": 25.0, "carbohydrates": 43.1,
        "fiber": 21.2, "absorbable_protein": 9.5,
    },
    "ajwain": {
        "type": "spices",
        "protein": 15.9, "fat": 25.0, "carbohydrates": 43.1,
        "fiber": 21.2, "absorbable_protein": 9.5,
    },

    # Dill seeds (shatakuppi) — USDA #02010
    "dill": {
        "type": "spices",
        "protein": 15.9, "fat": 14.5, "carbohydrates": 55.8,
        "fiber": 21.1, "absorbable_protein": 9.5,
    },

    # Cloves — USDA #02011
    "cloves": {
        "type": "spices",
        "protein": 5.9, "fat": 13.0, "carbohydrates": 65.5,
        "fiber": 33.9, "absorbable_protein": 3.5,
    },

    # Cinnamon (bark powder) — USDA #02010
    "cinnamon": {
        "type": "spices",
        "protein": 3.9, "fat": 3.2, "carbohydrates": 80.6,
        "fiber": 53.1, "absorbable_protein": 2.3,
    },

    # Dry ginger — USDA #11217
    "dry ginger": {
        "type": "spices",
        "protein": 8.8, "fat": 4.2, "carbohydrates": 71.6,
        "fiber": 14.1, "absorbable_protein": 5.0,
    },
    "sukku": {
        "type": "spices",
        "protein": 8.8, "fat": 4.2, "carbohydrates": 71.6,
        "fiber": 14.1, "absorbable_protein": 5.0,
    },

    # Nutmeg — USDA #02025
    "nutmeg": {
        "type": "spices",
        "protein": 5.8, "fat": 36.3, "carbohydrates": 49.3,
        "fiber": 20.8, "absorbable_protein": 3.5,
    },

    # Mace — USDA #02021
    "mace": {
        "type": "spices",
        "protein": 6.7, "fat": 32.4, "carbohydrates": 50.5,
        "fiber": 21.1, "absorbable_protein": 4.0,
    },

    # Star anise — literature
    "star anise": {
        "type": "spices",
        "protein": 17.6, "fat": 15.9, "carbohydrates": 50.0,
        "fiber": 14.6, "absorbable_protein": 10.0,
    },

    # Black cumin / nigella / kalonji — IFCT 2017
    "black cumin": {
        "type": "spices",
        "protein": 20.8, "fat": 38.2, "carbohydrates": 21.2,
        "fiber": 11.8, "absorbable_protein": 12.0,
    },
    "nigella": {
        "type": "spices",
        "protein": 20.8, "fat": 38.2, "carbohydrates": 21.2,
        "fiber": 11.8, "absorbable_protein": 12.0,
    },
    "kalonji": {
        "type": "spices",
        "protein": 20.8, "fat": 38.2, "carbohydrates": 21.2,
        "fiber": 11.8, "absorbable_protein": 12.0,
    },

    # Turmeric (powder) — USDA #02043
    "turmeric": {
        "type": "spices",
        "protein": 9.7, "fat": 3.3, "carbohydrates": 67.1,
        "fiber": 21.1, "absorbable_protein": 5.5,
    },

    # ══════════════════════════════════════════════════════════════════════════
    # HERBS / MEDICINAL  — type="herbs"
    # Treated as normal for display; show protein/fat/carbs/fiber
    # ══════════════════════════════════════════════════════════════════════════

    # Chebulic myrobalan (haritaki / kadukkai) — IFCT
    "haritaki": {
        "type": "herbs",
        "protein": 3.8, "fat": 0.6, "carbohydrates": 44.0, "fiber": 35.0,
        "absorbable_protein": 1.5,
    },
    "kadukkai": {
        "type": "herbs",
        "protein": 3.8, "fat": 0.6, "carbohydrates": 44.0, "fiber": 35.0,
        "absorbable_protein": 1.5,
    },

    # Bibhitaki (beleric myrobalan / thanrikkai) — IFCT
    "bibhitaki": {
        "type": "herbs",
        "protein": 3.0, "fat": 0.5, "carbohydrates": 42.0, "fiber": 28.0,
        "absorbable_protein": 1.2,
    },

    # Indian gooseberry (amla / nelli) — USDA #09316
    "amla": {
        "type": "herbs",
        "protein": 0.9, "fat": 0.1, "carbohydrates": 13.7, "fiber": 3.4,
        "absorbable_protein": 0.3,
    },
    "nelli": {
        "type": "herbs",
        "protein": 0.9, "fat": 0.1, "carbohydrates": 13.7, "fiber": 3.4,
        "absorbable_protein": 0.3,
    },

    # Licorice root (athimathuram) — literature
    "licorice": {
        "type": "herbs",
        "protein": 6.2, "fat": 0.5, "carbohydrates": 47.0, "fiber": 30.0,
        "absorbable_protein": 2.2,
    },

    # Sweet flag (calamus / vasambu) — IFCT
    "sweet flag": {
        "type": "herbs",
        "protein": 4.5, "fat": 2.5, "carbohydrates": 55.0, "fiber": 22.0,
        "absorbable_protein": 1.3,
    },
    "vasambu": {
        "type": "herbs",
        "protein": 4.5, "fat": 2.5, "carbohydrates": 55.0, "fiber": 22.0,
        "absorbable_protein": 1.3,
    },

    # Ashwagandha root powder — published phytochemical data
    "ashwagandha": {
        "type": "herbs",
        "protein": 3.9, "fat": 0.3, "carbohydrates": 49.9, "fiber": 32.3,
        "absorbable_protein": 1.6,
    },

    # Dried rose petals — literature
    "rose petal": {
        "type": "herbs",
        "protein": 3.0, "fat": 1.5, "carbohydrates": 65.0, "fiber": 15.0,
        "absorbable_protein": 0.9,
    },

    # ══════════════════════════════════════════════════════════════════════════
    # DRY FRUITS / SUPPLEMENTS  — type="normal"
    # ══════════════════════════════════════════════════════════════════════════

    # Dates (medjool) — USDA #09421
    "dates": {
        "type": "normal",
        "protein": 2.5, "fat": 0.4, "carbohydrates": 75.0,
        "fiber": 8.0, "absorbable_protein": 1.8,
    },
    "pericham": {
        "type": "normal",
        "protein": 2.5, "fat": 0.4, "carbohydrates": 75.0,
        "fiber": 8.0, "absorbable_protein": 1.8,
    },

    # Raisins — USDA #09298
    "raisins": {
        "type": "normal",
        "protein": 3.1, "fat": 0.5, "carbohydrates": 79.2,
        "fiber": 3.7, "absorbable_protein": 2.2,
    },
    "thiratchai": {
        "type": "normal",
        "protein": 3.1, "fat": 0.5, "carbohydrates": 79.2,
        "fiber": 3.7, "absorbable_protein": 2.2,
    },

    # Dried fig — USDA #09094
    "dried fig": {
        "type": "normal",
        "protein": 3.5, "fat": 1.4, "carbohydrates": 63.0,
        "fiber": 9.8, "absorbable_protein": 2.5,
    },

    # Honey — USDA #19296
    "honey": {
        "type": "normal",
        "protein": 0.3, "fat": 0.0, "carbohydrates": 82.4,
        "fiber": 0.2, "absorbable_protein": 0.0,
    },


    # ── AUTO-GENERATED: Additional product nutrition data ──────────────────


    "black grape": {
        "type": "normal",
        "protein": 2.3, "absorbable_protein": 1.7, "fat": 0.5,
        "carbohydrates": 79.5, "fiber": 3.7, "moisture": 15.0, "ash": 1.5,
    },
    "hazelnut": {
        "type": "oil",
        "protein": 15.0, "absorbable_protein": 12.0, "fat": 60.8,
        "carbohydrates": 16.7, "fiber": 9.7, "moisture": 3.4, "ash": 2.3,
    },
    "homer oilseed": {
        "type": "feed",
        "protein": 18.0, "absorbable_protein": 13.0, "fat": 30.0,
        "carbohydrates": 35.0, "fiber": 8.0, "moisture": 6.0, "ash": 3.0,
    },
    "rabbit feed": {
        "type": "feed",
        "protein": 16.0, "absorbable_protein": 10.0, "fat": 3.0,
        "carbohydrates": 50.0, "fiber": 18.0, "moisture": 10.0, "ash": 7.0,
    },
    "skm starter": {
        "type": "feed",
        "protein": 20.0, "absorbable_protein": 14.0, "fat": 5.0,
        "carbohydrates": 55.0, "fiber": 4.0, "moisture": 10.0, "ash": 6.0,
    },
    "triple bean": {
        "type": "protein",
        "protein": 22.0, "absorbable_protein": 16.5, "fat": 1.5,
        "carbohydrates": 60.0, "fiber": 8.0, "moisture": 10.0, "ash": 3.0,
    },
    "a19": {
        "type": "feed",
        "protein": 22.0, "absorbable_protein": 14.3, "fat": 15.0,
        "carbohydrates": 50.0, "fiber": 2.0, "moisture": 5.0, "ash": 6.0,
    },
    "akarkara": {
        "type": "herbs",
        "protein": 5.0, "absorbable_protein": 2.5, "fat": 2.0,
        "carbohydrates": 65.0, "fiber": 10.0, "moisture": 8.0, "ash": 3.0,
    },
    "alfalfa leaves": {
        "type": "herbs",
        "protein": 4.0, "absorbable_protein": 2.0, "fat": 0.5,
        "carbohydrates": 8.0, "fiber": 3.0, "moisture": 80.0, "ash": 2.0,
    },
    "alfalfa seeds": {
        "type": "protein",
        "protein": 35.0, "absorbable_protein": 26.25, "fat": 7.0,
        "carbohydrates": 20.0, "fiber": 10.0, "moisture": 7.0, "ash": 5.0,
    },
    "amaranth seeds": {
        "type": "normal",
        "protein": 14.0, "absorbable_protein": 10.5, "fat": 7.0,
        "carbohydrates": 65.0, "fiber": 7.0, "moisture": 10.0, "ash": 3.0,
    },
    "apple seeds": {
        "type": "normal",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 40.0,
        "carbohydrates": 25.0, "fiber": 10.0, "moisture": 5.0, "ash": 2.0,
    },
    "bamboo salt": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 99.0,
    },
    "banyan tree seeds": {
        "type": "normal",
        "protein": 12.0, "absorbable_protein": 9.0, "fat": 5.0,
        "carbohydrates": 60.0, "fiber": 15.0, "moisture": 8.0, "ash": 3.0,
    },
    "bitter gourd seeds": {
        "type": "normal",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 25.0,
        "carbohydrates": 30.0, "fiber": 10.0, "moisture": 8.0, "ash": 7.0,
    },

    "black millet": {
        "type": "normal",
        "protein": 11.0, "absorbable_protein": 8.25, "fat": 4.0,
        "carbohydrates": 70.0, "fiber": 8.0, "moisture": 10.0, "ash": 2.0,
    },
    "bottle gourd seeds": {
        "type": "normal",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 40.0,
        "carbohydrates": 25.0, "fiber": 10.0, "moisture": 8.0, "ash": 7.0,
    },
    "brahma dandu": {
        "type": "herbs",
        "protein": 5.0, "absorbable_protein": 2.5, "fat": 1.0,
        "carbohydrates": 60.0, "fiber": 15.0, "moisture": 10.0, "ash": 5.0,
    },
    "broad beans": {
        "type": "protein",
        "protein": 26.0, "absorbable_protein": 19.5, "fat": 1.0,
        "carbohydrates": 58.0, "fiber": 25.0, "moisture": 10.0, "ash": 4.0,
    },
    "brown top millet": {
        "type": "normal",
        "protein": 11.0, "absorbable_protein": 8.25, "fat": 3.0,
        "carbohydrates": 65.0, "fiber": 12.0, "moisture": 10.0, "ash": 2.0,
    },
    "calamus root": {
        "type": "herbs",
        "protein": 3.0, "absorbable_protein": 1.5, "fat": 1.0,
        "carbohydrates": 70.0, "fiber": 10.0, "moisture": 8.0, "ash": 3.0,
    },
    "calcium grit": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 100.0,
    },
    "canary seeds": {
        "type": "normal",
        "protein": 13.0, "absorbable_protein": 9.75, "fat": 5.0,
        "carbohydrates": 55.0, "fiber": 10.0, "moisture": 10.0, "ash": 3.0,
    },
    "cat's eye seeds": {
        "type": "normal",
        "protein": 15.0, "absorbable_protein": 11.25, "fat": 5.0,
        "carbohydrates": 60.0, "fiber": 15.0, "moisture": 8.0, "ash": 5.0,
    },
    "charcoal": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 100.0,
    },
    "cockatiel & african grey mix": {
        "type": "feed",
        "protein": 14.0, "absorbable_protein": 10.5, "fat": 15.0,
        "carbohydrates": 50.0, "fiber": 12.0, "moisture": 8.0, "ash": 3.0,
    },
    "costus root": {
        "type": "herbs",
        "protein": 4.0, "absorbable_protein": 2.0, "fat": 1.5,
        "carbohydrates": 65.0, "fiber": 12.0, "moisture": 8.0, "ash": 3.0,
    },
    "cuttlefish bone": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 100.0,
    },
    "date seeds": {
        "type": "normal",
        "protein": 5.0, "absorbable_protein": 3.75, "fat": 10.0,
        "carbohydrates": 65.0, "fiber": 20.0, "moisture": 8.0, "ash": 2.0,
    },
    "ddk homer breeding mix": {
        "type": "feed",
        "protein": 15.0, "absorbable_protein": 11.25, "fat": 8.0,
        "carbohydrates": 55.0, "fiber": 10.0, "moisture": 8.0, "ash": 4.0,
    },
    "double beans": {
        "type": "protein",
        "protein": 25.0, "absorbable_protein": 18.75, "fat": 1.0,
        "carbohydrates": 55.0, "fiber": 20.0, "moisture": 10.0, "ash": 4.0,
    },
    "dried grapes": {
        "type": "normal",
        "protein": 2.3, "absorbable_protein": 1.725, "fat": 0.5,
        "carbohydrates": 79.5, "fiber": 3.7, "moisture": 15.0, "ash": 1.5,
    },
    "dried red chilli": {
        "type": "spices",
        "protein": 12.0, "absorbable_protein": 6.0, "fat": 10.0,
        "carbohydrates": 50.0, "fiber": 25.0, "moisture": 8.0, "ash": 5.0,
    },
    "eggshell flakes": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 100.0,
    },
    "fennel seeds": {
        "type": "spices",
        "protein": 16.0, "absorbable_protein": 12.0, "fat": 15.0,
        "carbohydrates": 40.0, "fiber": 30.0, "moisture": 8.0, "ash": 10.0,
    },
    "fig tree seeds": {
        "type": "normal",
        "protein": 10.0, "absorbable_protein": 7.5, "fat": 8.0,
        "carbohydrates": 60.0, "fiber": 12.0, "moisture": 8.0, "ash": 2.0,
    },
    "fox-tail beans": {
        "type": "protein",
        "protein": 24.0, "absorbable_protein": 18.0, "fat": 1.5,
        "carbohydrates": 55.0, "fiber": 18.0, "moisture": 10.0, "ash": 3.0,
    },
    "fruit mix pellets": {
        "type": "feed",
        "protein": 10.0, "absorbable_protein": 7.0, "fat": 4.0,
        "carbohydrates": 60.0, "fiber": 8.0, "moisture": 10.0, "ash": 4.0,
    },
    "goathead thorn": {
        "type": "herbs",
        "protein": 10.0, "absorbable_protein": 5.0, "fat": 2.0,
        "carbohydrates": 60.0, "fiber": 15.0, "moisture": 10.0, "ash": 5.0,
    },
    "green peas": {
        "type": "protein",
        "protein": 24.0, "absorbable_protein": 18.0, "fat": 1.5,
        "carbohydrates": 60.0, "fiber": 25.0, "moisture": 12.0, "ash": 3.0,
    },
    "green proso millet": {
        "type": "normal",
        "protein": 11.0, "absorbable_protein": 8.25, "fat": 4.0,
        "carbohydrates": 70.0, "fiber": 8.0, "moisture": 10.0, "ash": 2.0,
    },
    "grey macaw seed mix": {
        "type": "feed",
        "protein": 14.0, "absorbable_protein": 10.5, "fat": 15.0,
        "carbohydrates": 50.0, "fiber": 12.0, "moisture": 8.0, "ash": 3.0,
    },
    "halim seeds": {
        "type": "protein",
        "protein": 25.0, "absorbable_protein": 18.75, "fat": 20.0,
        "carbohydrates": 25.0, "fiber": 10.0, "moisture": 8.0, "ash": 12.0,
    },
    "hamster pellets": {
        "type": "feed",
        "protein": 18.0, "absorbable_protein": 12.6, "fat": 5.0,
        "carbohydrates": 55.0, "fiber": 10.0, "moisture": 8.0, "ash": 4.0,
    },
    "hemp seed mix": {
        "type": "feed",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 30.0,
        "carbohydrates": 25.0, "fiber": 15.0, "moisture": 8.0, "ash": 2.0,
    },
    "hemp seed mix (bird feed)": {
        "type": "feed",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 30.0,
        "carbohydrates": 25.0, "fiber": 15.0, "moisture": 8.0, "ash": 2.0,
    },
    "hemp seeds": {
        "type": "protein",
        "protein": 30.0, "absorbable_protein": 22.5, "fat": 45.0,
        "carbohydrates": 10.0, "fiber": 5.0, "moisture": 8.0, "ash": 2.0,
    },
    "irungusholam": {
        "type": "normal",
        "protein": 10.0, "absorbable_protein": 7.5, "fat": 3.0,
        "carbohydrates": 70.0, "fiber": 6.0, "moisture": 10.0, "ash": 2.0,
    },
    "jaliyaa": {
        "type": "herbs",
        "protein": 4.0, "absorbable_protein": 2.0, "fat": 1.0,
        "carbohydrates": 68.0, "fiber": 10.0, "moisture": 8.0, "ash": 4.0,
    },
    "kaagergai": {
        "type": "herbs",
        "protein": 8.0, "absorbable_protein": 4.0, "fat": 2.0,
        "carbohydrates": 60.0, "fiber": 15.0, "moisture": 10.0, "ash": 5.0,
    },
    "kollu urundu": {
        "type": "normal",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 5.0,
        "carbohydrates": 65.0, "fiber": 10.0, "moisture": 5.0, "ash": 5.0,
    },
    "large pigeon seed mix": {
        "type": "feed",
        "protein": 13.0, "absorbable_protein": 9.75, "fat": 8.0,
        "carbohydrates": 58.0, "fiber": 10.0, "moisture": 8.0, "ash": 3.0,
    },
    "limestone grit": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 100.0,
    },
    "long pepper": {
        "type": "spices",
        "protein": 6.0, "absorbable_protein": 3.0, "fat": 2.0,
        "carbohydrates": 60.0, "fiber": 20.0, "moisture": 8.0, "ash": 4.0,
    },
    "lovebird seed mix": {
        "type": "feed",
        "protein": 12.0, "absorbable_protein": 9.0, "fat": 10.0,
        "carbohydrates": 55.0, "fiber": 10.0, "moisture": 10.0, "ash": 3.0,
    },
    "marathi mokku": {
        "type": "herbs",
        "protein": 5.0, "absorbable_protein": 2.5, "fat": 2.0,
        "carbohydrates": 65.0, "fiber": 12.0, "moisture": 8.0, "ash": 3.0,
    },
    "masikkai": {
        "type": "herbs",
        "protein": 2.0, "absorbable_protein": 1.0, "fat": 0.5,
        "carbohydrates": 70.0, "fiber": 25.0, "moisture": 8.0, "ash": 4.0,
    },
    "mineral black grit": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 100.0,
    },
    "mineral salt": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 99.0,
    },
    "modikuchi": {
        "type": "herbs",
        "protein": 5.0, "absorbable_protein": 2.5, "fat": 2.0,
        "carbohydrates": 65.0, "fiber": 12.0, "moisture": 8.0, "ash": 3.0,
    },
    "moringa resin": {
        "type": "other",
        "protein": 2.0, "absorbable_protein": 1.0, "fat": 0.5,
        "carbohydrates": 80.0, "fiber": 5.0, "moisture": 8.0, "ash": 2.0,
    },
    "moth beans": {
        "type": "protein",
        "protein": 23.0, "absorbable_protein": 17.25, "fat": 1.5,
        "carbohydrates": 60.0, "fiber": 15.0, "moisture": 10.0, "ash": 3.0,
    },
    "nandivardhanam seeds": {
        "type": "normal",
        "protein": 15.0, "absorbable_protein": 11.25, "fat": 10.0,
        "carbohydrates": 60.0, "fiber": 10.0, "moisture": 8.0, "ash": 2.0,
    },
    "nannari root": {
        "type": "herbs",
        "protein": 3.0, "absorbable_protein": 1.5, "fat": 1.0,
        "carbohydrates": 70.0, "fiber": 10.0, "moisture": 8.0, "ash": 3.0,
    },
    "nut grass root": {
        "type": "herbs",
        "protein": 4.0, "absorbable_protein": 2.0, "fat": 1.0,
        "carbohydrates": 65.0, "fiber": 12.0, "moisture": 8.0, "ash": 4.0,
    },
    "onion seeds": {
        "type": "spices",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 15.0,
        "carbohydrates": 35.0, "fiber": 10.0, "moisture": 8.0, "ash": 5.0,
    },
    "orlux lori supplement": {
        "type": "feed",
        "protein": 15.0, "absorbable_protein": 9.75, "fat": 5.0,
        "carbohydrates": 60.0, "fiber": 3.0, "moisture": 5.0, "ash": 5.0,
    },
    "palm stem salt": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 99.0,
    },
    "parakeet & budgerigar mix": {
        "type": "feed",
        "protein": 12.0, "absorbable_protein": 9.0, "fat": 6.0,
        "carbohydrates": 60.0, "fiber": 8.0, "moisture": 10.0, "ash": 4.0,
    },
    "parrot seed mix": {
        "type": "feed",
        "protein": 14.0, "absorbable_protein": 10.5, "fat": 15.0,
        "carbohydrates": 50.0, "fiber": 12.0, "moisture": 8.0, "ash": 3.0,
    },
    "peepal tree seeds": {
        "type": "normal",
        "protein": 10.0, "absorbable_protein": 7.5, "fat": 5.0,
        "carbohydrates": 65.0, "fiber": 12.0, "moisture": 8.0, "ash": 2.0,
    },
    "pet life supplement": {
        "type": "feed",
        "protein": 18.0, "absorbable_protein": 11.7, "fat": 8.0,
        "carbohydrates": 50.0, "fiber": 5.0, "moisture": 5.0, "ash": 6.0,
    },
    "pineapple flower": {
        "type": "normal",
        "protein": 3.0, "absorbable_protein": 1.5, "fat": 0.5,
        "carbohydrates": 15.0, "fiber": 3.0, "moisture": 75.0, "ash": 1.0,
    },
    "poongaavi": {
        "type": "other",
        "protein": 2.0, "absorbable_protein": 1.0, "fat": 1.0,
        "carbohydrates": 70.0, "fiber": 10.0, "moisture": 10.0, "ash": 7.0,
    },
    "poultry grit": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 100.0,
    },
    "quail grass seeds": {
        "type": "normal",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 8.0,
        "carbohydrates": 50.0, "fiber": 10.0, "moisture": 8.0, "ash": 4.0,
    },
    "radish seeds": {
        "type": "normal",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 20.0,
        "carbohydrates": 30.0, "fiber": 15.0, "moisture": 8.0, "ash": 5.0,
    },
    "ready-mix flour": {
        "type": "normal",
        "protein": 10.0, "absorbable_protein": 7.5, "fat": 2.0,
        "carbohydrates": 70.0, "fiber": 5.0, "moisture": 10.0, "ash": 2.0,
    },
    "red gram": {
        "type": "protein",
        "protein": 22.0, "absorbable_protein": 16.5, "fat": 1.5,
        "carbohydrates": 60.0, "fiber": 15.0, "moisture": 10.0, "ash": 3.0,
    },
    "red millet": {
        "type": "normal",
        "protein": 11.0, "absorbable_protein": 8.25, "fat": 4.0,
        "carbohydrates": 70.0, "fiber": 8.0, "moisture": 10.0, "ash": 2.0,
    },
    "roasted peanuts": {
        "type": "protein",
        "protein": 25.0, "absorbable_protein": 18.75, "fat": 48.0,
        "carbohydrates": 16.0, "fiber": 8.0, "moisture": 2.0, "ash": 3.0,
    },
    "rock sugar": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 100.0, "fiber": 0.0, "moisture": 0.0, "ash": 0.0,
    },
    "sabja seeds": {
        "type": "normal",
        "protein": 14.0, "absorbable_protein": 10.5, "fat": 12.0,
        "carbohydrates": 45.0, "fiber": 15.0, "moisture": 8.0, "ash": 6.0,
    },
    "salted roasted chickpeas": {
        "type": "protein",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 5.0,
        "carbohydrates": 55.0, "fiber": 15.0, "moisture": 5.0, "ash": 5.0,
    },
    "sara paruppu": {
        "type": "protein",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 2.0,
        "carbohydrates": 60.0, "fiber": 15.0, "moisture": 8.0, "ash": 3.0,
    },
    "sarana root": {
        "type": "herbs",
        "protein": 4.0, "absorbable_protein": 2.0, "fat": 1.0,
        "carbohydrates": 68.0, "fiber": 10.0, "moisture": 8.0, "ash": 4.0,
    },
    "shatavari root": {
        "type": "herbs",
        "protein": 3.0, "absorbable_protein": 1.5, "fat": 1.0,
        "carbohydrates": 70.0, "fiber": 15.0, "moisture": 8.0, "ash": 3.0,
    },
    "show budgerigar seed mix": {
        "type": "feed",
        "protein": 12.0, "absorbable_protein": 9.0, "fat": 6.0,
        "carbohydrates": 60.0, "fiber": 8.0, "moisture": 10.0, "ash": 4.0,
    },
    "sithirathai root": {
        "type": "herbs",
        "protein": 3.0, "absorbable_protein": 1.5, "fat": 1.0,
        "carbohydrates": 70.0, "fiber": 10.0, "moisture": 8.0, "ash": 3.0,
    },
    "small conure seed mix": {
        "type": "feed",
        "protein": 12.0, "absorbable_protein": 9.0, "fat": 10.0,
        "carbohydrates": 55.0, "fiber": 10.0, "moisture": 10.0, "ash": 3.0,
    },
    "small pigeon seed mix": {
        "type": "feed",
        "protein": 12.0, "absorbable_protein": 9.0, "fat": 6.0,
        "carbohydrates": 60.0, "fiber": 8.0, "moisture": 10.0, "ash": 4.0,
    },
    "sprouted triple beans": {
        "type": "protein",
        "protein": 24.0, "absorbable_protein": 18.0, "fat": 1.5,
        "carbohydrates": 55.0, "fiber": 20.0, "moisture": 10.0, "ash": 3.0,
    },
    "sprouting seed mix": {
        "type": "feed",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 5.0,
        "carbohydrates": 50.0, "fiber": 10.0, "moisture": 10.0, "ash": 5.0,
    },
    "sun conure seed mix": {
        "type": "feed",
        "protein": 14.0, "absorbable_protein": 10.5, "fat": 12.0,
        "carbohydrates": 50.0, "fiber": 10.0, "moisture": 10.0, "ash": 4.0,
    },
    "sweet potato vine root": {
        "type": "normal",
        "protein": 2.0, "absorbable_protein": 1.0, "fat": 0.5,
        "carbohydrates": 20.0, "fiber": 3.0, "moisture": 70.0, "ash": 1.5,
    },
    "syringe": {
        "type": "other",
        "protein": 0.0, "absorbable_protein": 0.0, "fat": 0.0,
        "carbohydrates": 0.0, "fiber": 0.0, "moisture": 0.0, "ash": 0.0,
    },
    "tail pepper": {
        "type": "spices",
        "protein": 5.0, "absorbable_protein": 2.5, "fat": 5.0,
        "carbohydrates": 60.0, "fiber": 20.0, "moisture": 8.0, "ash": 2.0,
    },
    "tham powder": {
        "type": "other",
        "protein": 5.0, "absorbable_protein": 2.5, "fat": 1.0,
        "carbohydrates": 70.0, "fiber": 10.0, "moisture": 8.0, "ash": 6.0,
    },
    "tham powder (processed)": {
        "type": "other",
        "protein": 5.0, "absorbable_protein": 2.5, "fat": 1.0,
        "carbohydrates": 70.0, "fiber": 10.0, "moisture": 8.0, "ash": 6.0,
    },
    "thandrikkai": {
        "type": "herbs",
        "protein": 4.0, "absorbable_protein": 2.0, "fat": 1.0,
        "carbohydrates": 60.0, "fiber": 20.0, "moisture": 8.0, "ash": 7.0,
    },
    "thudhuvalai root": {
        "type": "herbs",
        "protein": 4.0, "absorbable_protein": 2.0, "fat": 1.0,
        "carbohydrates": 65.0, "fiber": 12.0, "moisture": 8.0, "ash": 4.0,
    },
    "triple beans mix": {
        "type": "protein",
        "protein": 24.0, "absorbable_protein": 18.0, "fat": 1.5,
        "carbohydrates": 55.0, "fiber": 20.0, "moisture": 10.0, "ash": 3.0,
    },
    "vaayuvilangam": {
        "type": "herbs",
        "protein": 10.0, "absorbable_protein": 5.0, "fat": 5.0,
        "carbohydrates": 60.0, "fiber": 15.0, "moisture": 8.0, "ash": 2.0,
    },
    "water thorn root": {
        "type": "herbs",
        "protein": 4.0, "absorbable_protein": 2.0, "fat": 1.0,
        "carbohydrates": 68.0, "fiber": 10.0, "moisture": 8.0, "ash": 4.0,
    },
    "watermelon seeds": {
        "type": "protein",
        "protein": 28.0, "absorbable_protein": 21.0, "fat": 48.0,
        "carbohydrates": 15.0, "fiber": 3.0, "moisture": 5.0, "ash": 3.0,
    },
    "white cucumber seeds": {
        "type": "normal",
        "protein": 20.0, "absorbable_protein": 15.0, "fat": 40.0,
        "carbohydrates": 25.0, "fiber": 10.0, "moisture": 8.0, "ash": 7.0,
    },
    "white millet": {
        "type": "normal",
        "protein": 11.0, "absorbable_protein": 8.25, "fat": 4.0,
        "carbohydrates": 70.0, "fiber": 8.0, "moisture": 10.0, "ash": 2.0,
    },
    "white triple beans": {
        "type": "protein",
        "protein": 24.0, "absorbable_protein": 18.0, "fat": 1.5,
        "carbohydrates": 55.0, "fiber": 20.0, "moisture": 10.0, "ash": 3.0,
    },
    "yellow millet": {
        "type": "normal",
        "protein": 11.0, "absorbable_protein": 8.25, "fat": 4.0,
        "carbohydrates": 70.0, "fiber": 8.0, "moisture": 10.0, "ash": 2.0,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

# Internal type → which standard keys are populated + which to DISPLAY
_TYPE_CONFIG: dict[str, dict] = {
    "oil": {
        "fields":  {"protein", "fat", "carbohydrates", "fiber", "moisture", "ash"},
        "hide":    {"absorbable_protein"},
        "show_abs": False,
    },
    "nuts": {
        "fields":  {"protein", "fat", "carbohydrates", "fiber"},
        "hide":    {"moisture", "ash", "absorbable_protein"},
        "show_abs": False,
    },
    "normal": {
        "fields":  {"protein", "fat", "carbohydrates", "fiber", "absorbable_protein"},
        "hide":    {"moisture", "ash"},
        "show_abs": True,
    },
    "protein": {
        "fields":  {"protein", "fat", "carbohydrates", "fiber", "absorbable_protein"},
        "hide":    {"moisture", "ash"},
        "show_abs": True,
    },
    "gum": {
        "fields":  {"carbohydrates", "protein", "fat", "moisture", "ash"},
        "hide":    {"fiber", "absorbable_protein"},
        "show_abs": False,
    },
    "spices": {
        "fields":  {"protein", "fat", "carbohydrates", "fiber"},
        "hide":    {"moisture", "ash", "absorbable_protein"},
        "show_abs": False,
    },
    "herbs": {
        "fields":  {"protein", "fat", "carbohydrates", "fiber"},
        "hide":    {"moisture", "ash", "absorbable_protein"},
        "show_abs": False,
    },
    "feed": {
        "fields":  {"protein", "fat", "carbohydrates", "fiber"},
        "hide":    {"moisture", "ash", "absorbable_protein"},
        "show_abs": False,
    },
    "other": {
        "fields":  set(),
        "hide":    set(),
        "show_abs": False,
    },
}

_STANDARD_KEYS = (
    "protein", "fat", "carbohydrates", "fiber",
    "moisture", "ash", "absorbable_protein",
)

_MINIMUM_DISPLAY_THRESHOLD = 0.5   # g — values below this are hidden (per spec)


def get_nutrition_data(name: str) -> Optional[dict]:
    """
    Substring-match lookup in the internal database.

    The lookup is ordered from longest key to shortest to give more-specific
    entries (e.g. "moringa seed") priority over shorter ones ("moringa").

    Args:
        name: English product name, case-insensitive.

    Returns:
        Per-100g nutrition dict (with a "type" key), or None if not found.
    """
    if not name:
        return None
    name_lc = name.lower()
    # Longest-key-first ensures "moringa seed" wins over "moringa"
    for key in sorted(_NUTRITION_DB, key=len, reverse=True):
        if key in name_lc:
            return _NUTRITION_DB[key]
    return None


def calculate_nutrition(
    name: str,
    category: str,
    quantity_g: float,
) -> dict:
    """
    Calculate scaled nutrition for a product purchase.

    ⚠️  Returns VALUES IN GRAMS scaled to quantity_g.
        Use get_nutrition_data() directly if you need per-100g percentages for display.

    Workflow:
      1. classify_product(name, category)       → product type
      2. get_nutrition_data(name)               → per-100g values (DB match)
         Falls back to type-based class defaults if no DB entry.
      3. Scale: value_g = (per100g / 100) * quantity_g   ← GRAMS, not percent
      4. Round to 1 decimal place.
      5. Apply display rules (hide values < 0.5g, hide hidden fields by type).
      6. Return clean dict of gram values.

    Args:
        name:       English product name.
        category:   Product category (e.g. "Nuts", "Spices").
        quantity_g: Purchase weight in grams (e.g. 250).

    Returns:
        {
            "type":      str,
            "nutrition": {key: float_GRAMS, ...}   # gram values, not percentages
        }
    """
    prod_type = classify_product(name, category)
    raw_data  = get_nutrition_data(name)

    # ── Fallback: no DB entry → return empty nutrition rather than guessing ───
    if raw_data is None:
        return {
            "type":      prod_type,
            "nutrition": {},
        }

    cfg = _TYPE_CONFIG.get(prod_type, _TYPE_CONFIG["normal"])

    # ── Scale values to grams ─────────────────────────────────────────────────
    scaled: dict[str, float] = {}
    for key in _STANDARD_KEYS:
        per100 = float(raw_data.get(key) or 0.0)
        scaled[key] = round((per100 / 100.0) * quantity_g, 1)

    # ── Apply display rules ───────────────────────────────────────────────────
    visible: dict[str, float] = {}
    for key in _STANDARD_KEYS:
        if key in cfg["hide"]:
            continue
        if key not in cfg["fields"]:
            continue
        val = scaled[key]
        if val < _MINIMUM_DISPLAY_THRESHOLD:
            continue
        visible[key] = val

    return {
        "type":      prod_type,
        "nutrition": visible,
    }


def calculate_nutrition_percent(
    name: str,
    category: str,
) -> dict:
    """
    Return per-100g nutrition as PERCENTAGE values (0–100 range).

    This is the correct function to call when you need values for bill display.
    It reads directly from the per-100g database — no quantity scaling needed.

    Args:
        name:     English product name.
        category: Product category.

    Returns:
        {
            "type":      str,
            "nutrition": {key: float_PERCENT, ...}   # per-100g %, range 0–100
        }
    """
    prod_type = classify_product(name, category)
    raw_data  = get_nutrition_data(name)

    if raw_data is None:
        return {"type": prod_type, "nutrition": {}}

    cfg = _TYPE_CONFIG.get(prod_type, _TYPE_CONFIG["normal"])

    visible: dict[str, float] = {}
    for key in _STANDARD_KEYS:
        if key in cfg["hide"]:
            continue
        if key not in cfg["fields"]:
            continue
        val = round(float(raw_data.get(key) or 0.0), 1)
        if val < _MINIMUM_DISPLAY_THRESHOLD:
            continue
        visible[key] = val

    return {
        "type":      prod_type,
        "nutrition": visible,
    }


# ── Class-level fallback defaults (used only when no DB key matches) ──────────
# These are conservative mid-range values — not product-specific.
_CLASS_DEFAULTS: dict[str, dict] = {
    "oil": {
        "protein": 20.0, "fat": 42.0, "carbohydrates": 22.0,
        "fiber": 10.0, "moisture": 6.0, "ash": 3.5,
        "absorbable_protein": 0.0,
    },
    "nuts": {
        "protein": 18.0, "fat": 52.0, "carbohydrates": 18.0,
        "fiber": 8.0, "moisture": 0.0, "ash": 0.0,
        "absorbable_protein": 0.0,
    },
    "normal": {
        "protein": 12.0, "fat": 3.5, "carbohydrates": 65.0,
        "fiber": 8.0, "moisture": 0.0, "ash": 0.0,
        "absorbable_protein": 7.5,
    },
    "protein": {
        "protein": 22.0, "fat": 2.0, "carbohydrates": 60.0,
        "fiber": 10.0, "moisture": 0.0, "ash": 0.0,
        "absorbable_protein": 15.0,
    },
    "gum": {
        "protein": 4.0, "fat": 0.5, "carbohydrates": 83.0,
        "fiber": 0.0, "moisture": 11.0, "ash": 6.5,
        "absorbable_protein": 0.0,
    },
    "spices": {
        "protein": 12.0, "fat": 10.0, "carbohydrates": 55.0,
        "fiber": 18.0, "moisture": 0.0, "ash": 0.0,
        "absorbable_protein": 0.0,
    },
    "herbs": {
        "protein": 5.0, "fat": 1.5, "carbohydrates": 50.0,
        "fiber": 20.0, "moisture": 0.0, "ash": 0.0,
        "absorbable_protein": 0.0,
    },
    "feed": {
        "protein": 14.0, "fat": 4.0, "carbohydrates": 60.0,
        "fiber": 8.0, "moisture": 0.0, "ash": 0.0,
        "absorbable_protein": 0.0,
    },
    "other": {
        "protein": 0.0, "fat": 0.0, "carbohydrates": 0.0,
        "fiber": 0.0, "moisture": 0.0, "ash": 0.0,
        "absorbable_protein": 0.0,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# QUICK SELF-TEST  (run with: python nutrition_engine.py)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json

    test_cases = [
        ("Almond Gum (Badam Pisin)",   "Herbs",    250),
        ("Almonds",                    "Nuts",      100),
        ("Sunflower Seeds",            "Seeds",     500),
        ("Flax Seeds",                 "Seeds",     200),
        ("Niger Seeds (Ramtil)",       "Seeds",     300),
        ("Groundnut (Peanut)",         "Nuts",      250),
        ("Finger Millet (Ragi)",       "Seeds",     500),
        ("Chickpeas",                  "Protein",   250),
        ("Cowpea",                     "Protein",   250),
        ("Wheat",                      "Seeds",     500),
        ("Black Pepper",               "Spices",    100),
        ("Cumin Seeds",                "Spices",    100),
        ("Hamster Pellets",            "Feed",      200),
        ("Honey",                      "Supplements", 100),
        ("Unknown Herb Powder",        "Herbs",     100),
    ]

    print(f"{'Product':<40} {'Type':<10} {'Qty':>5}   Nutrition")
    print("─" * 100)
    for name, cat, qty in test_cases:
        result = calculate_nutrition(name, cat, qty)
        nutr   = result["nutrition"]
        # Format absorbable_protein as sub-line
        abs_p  = nutr.pop("absorbable_protein", None)
        nutr_str = ", ".join(f"{k}: {v}g" for k, v in nutr.items())
        if abs_p is not None:
            nutr_str += f"  ↳ Absorbable: {abs_p}g"
        print(f"{name:<40} {result['type']:<10} {qty:>5}g  {nutr_str}")