"""
DHANA DHANYA KADAI — Billing Backend  |  app.py  v20
Flask + SQLite  –  all data persists in billing.db

Changes in v20:
- Replaced legacy PRODUCT_NUTRITION_DB / classify_product / etc.
  with nutrition_engine.py
- PyInstaller EXE compatibility: all paths use resource_path / data_path
"""

import os
import sys
import sqlite3
import uuid
import base64
import json
import io
import re
import logging
import shutil
import threading
from functools import wraps
from hashlib import sha256
from datetime import datetime, timedelta

from flask import (Flask, request, jsonify, send_from_directory,
                   send_file, Response, session, redirect, render_template)
from flask_cors import CORS
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment


# ═══════════════════════════════════════════════════════
# PATH HELPERS — PyInstaller EXE compatible
# ═══════════════════════════════════════════════════════

def resource_path(relative_path):
    """Return absolute path for READ-ONLY bundled assets (templates, static, frontend).
    Uses sys._MEIPASS inside a PyInstaller EXE.
    In dev: anchored to the PARENT of this file (backend/../ = Billing_system_v7/)
    so that resource_path('frontend') always resolves correctly regardless of cwd."""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        # Go one level up from backend/ to reach Billing_system_v7/
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)


def data_path(relative_path):
    """Return absolute path for WRITABLE / persistent data (DB, images, counters).
    Inside EXE: next to the .exe file.
    In dev: anchored to the backend/ folder (where billing.db lives)."""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)


def ensure_data_dirs():
    """Create writable data directories if they don't exist (first EXE run)."""
    dirs = [
        data_path(""),
        data_path("product_images"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)


# Call once at import time
ensure_data_dirs()


# ── Safe nutrition_engine import (works in EXE and dev) ───────────────────────
# data_path() now points to backend/ itself in dev mode; insert directly
_backend_dir = data_path("")   # -> .../Billing_system_v7/backend/
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from nutrition_engine import (
    classify_product,
    get_nutrition_data,
    calculate_nutrition     as _calc_nutrition_engine,
    calculate_nutrition_percent as _calc_nutrition_pct,
)


# ── Safe float parser ─────────────────────────────────────────────────────────
def get_float(value):
    """Safely parse a value to float, return 0.0 on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ── Money arithmetic (AUDIT FIX F7) ──────────────────────────────────────────
# Line amounts are computed in exact decimal and rounded half-up to paise.
# frontend/script.js::calcPrice() implements the identical algorithm so the
# cart display and the saved bill can never differ by a paisa.
from decimal import Decimal, ROUND_HALF_UP
import math as _math

_PAISA = Decimal('0.01')


def money(value):
    """Round a number to 2 decimals, half-up, using its shortest decimal repr."""
    return float(Decimal(repr(float(value))).quantize(_PAISA, rounding=ROUND_HALF_UP))


def line_amount(price_per_kg, weight_g):
    """Exact price_per_kg × weight_g / 1000, rounded half-up to paise."""
    d = Decimal(repr(float(price_per_kg))) * Decimal(repr(float(weight_g))) / Decimal(1000)
    return float(d.quantize(_PAISA, rounding=ROUND_HALF_UP))


class BillValidationError(ValueError):
    """Raised for a bad /bill payload; mapped to HTTP 400 before any DB write."""


def _finite_number(value, field, minimum=None, maximum=None, allow_equal_min=True):
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise BillValidationError(f'{field} must be a number')
    if not _math.isfinite(num):
        raise BillValidationError(f'{field} must be a finite number')
    if minimum is not None and (num < minimum or (num == minimum and not allow_equal_min)):
        raise BillValidationError(f'{field} must be {">=" if allow_equal_min else ">"} {minimum}')
    if maximum is not None and num > maximum:
        raise BillValidationError(f'{field} must be <= {maximum}')
    return num


def normalize_phone(raw):
    """Digits-only customer key — identical to the frontend lookup (non-digits stripped)."""
    return re.sub(r'\D', '', str(raw or ''))


# Chronological sort key for created_at stored as 'DD/MM/YYYY HH:MM:SS'
# (plain ORDER BY created_at sorts by day-of-month first — AUDIT FIX F8).
CREATED_AT_SORT_SQL = (
    "(substr(created_at,7,4)||'-'||substr(created_at,4,2)||'-'||"
    "substr(created_at,1,2)||' '||substr(created_at,12,8))"
)

# ── Sequential bill numbering ─────────────────────────────────────────────────
# NOTE: bill_counter.txt is no longer used. Bill numbers are stored in SQLite so
# they stay stable across restarts, PCs, and PyInstaller/pywebview runs.

BILL_NUMBER_START_KEY = 'bill_number_start'
BILL_NUMBER_NEXT_KEY  = 'bill_number_next'


def _read_int_setting(conn, key, default=1):
    row = conn.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
    try:
        return int(row['value'] if hasattr(row, 'keys') else row[0]) if row else default
    except (TypeError, ValueError):
        return default


def _bill_number_exists(conn, bill_no):
    return conn.execute('SELECT 1 FROM bills WHERE id=? LIMIT 1', (str(bill_no),)).fetchone() is not None


def peek_next_bill_no(conn=None):
    """Return the next visible bill number without changing the counter."""
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True
    try:
        candidate = max(1, _read_int_setting(
            conn,
            BILL_NUMBER_NEXT_KEY,
            _read_int_setting(conn, BILL_NUMBER_START_KEY, 1)
        ))
        while _bill_number_exists(conn, candidate):
            candidate += 1
        return candidate
    finally:
        if close_conn:
            conn.close()


def reserve_next_bill_no(conn=None):
    """Reserve and persist the next sequential bill number.

    This deliberately does NOT use MAX(id) because older builds could save
    random/timestamp-like IDs. Those old rows must not force future bills to
    continue from a random number such as 88266049.
    """
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True
    try:
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        next_id = peek_next_bill_no(conn)
        conn.execute(
            'INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)',
            (BILL_NUMBER_NEXT_KEY, str(next_id + 1))
        )
        if close_conn:
            conn.commit()
        return next_id
    finally:
        if close_conn:
            conn.close()


# Backward-compatible read-only name used by reset/status code.
def get_next_bill_no(conn=None):
    return peek_next_bill_no(conn)


# ── Tamil → Tanglish auto-generation map ─────────────────────────────────────
# Maps common Tamil characters/syllables to their Tanglish (phonetic) equivalents
TAMIL_TANGLISH_MAP = {
    'அ':'a','ஆ':'aa','இ':'i','ஈ':'ii','உ':'u','ஊ':'uu','எ':'e','ஏ':'ee',
    'ஐ':'ai','ஒ':'o','ஓ':'oo','ஔ':'au',
    'க்':'k','ங்':'ng','ச்':'ch','ஞ்':'nj','ட்':'t','ண்':'n',
    'த்':'th','ந்':'n','ப்':'p','ம்':'m','ய்':'y','ர்':'r','ல்':'l',
    'வ்':'v','ழ்':'zh','ள்':'l','ற்':'r','ன்':'n','ஜ்':'j','ஷ்':'sh',
    'ஸ்':'s','ஹ்':'h',
    'க':'ka','கா':'kaa','கி':'ki','கீ':'kii','கு':'ku','கூ':'kuu',
    'கெ':'ke','கே':'kee','கை':'kai','கொ':'ko','கோ':'koo',
    'ச':'cha','சா':'chaa','சி':'chi','சீ':'chii','சு':'chu','சூ':'chuu',
    'செ':'che','சே':'chee','சை':'chai','சொ':'cho','சோ':'choo',
    'ட':'ta','டா':'taa','டி':'ti','டீ':'tii','டு':'tu','டூ':'tuu',
    'டெ':'te','டே':'tee','டொ':'to','டோ':'too',
    'த':'tha','தா':'thaa','தி':'thi','தீ':'thii','து':'thu','தூ':'thuu',
    'தெ':'the','தே':'thee','தை':'thai','தொ':'tho','தோ':'thoo',
    'ப':'pa','பா':'paa','பி':'pi','பீ':'pii','பு':'pu','பூ':'puu',
    'பெ':'pe','பே':'pee','பை':'pai','பொ':'po','போ':'poo',
    'ம':'ma','மா':'maa','மி':'mi','மீ':'mii','மு':'mu','மூ':'muu',
    'மெ':'me','மே':'mee','மை':'mai','மொ':'mo','மோ':'moo',
    'ர':'ra','ரா':'raa','ரி':'ri','ரீ':'rii','ரு':'ru','ரூ':'ruu',
    'ரெ':'re','ரே':'ree','ரை':'rai','ரொ':'ro','ரோ':'roo',
    'ல':'la','லா':'laa','லி':'li','லீ':'lii','லு':'lu','லூ':'luu',
    'லெ':'le','லே':'lee','லை':'lai','லொ':'lo','லோ':'loo',
    'வ':'va','வா':'vaa','வி':'vi','வீ':'vii','வு':'vu','வூ':'vuu',
    'வெ':'ve','வே':'vee','வை':'vai','வொ':'vo','வோ':'voo',
    'ந':'na','நா':'naa','நி':'ni','நீ':'nii','நு':'nu','நூ':'nuu',
    'நெ':'ne','நே':'nee','நை':'nai','நொ':'no','நோ':'noo',
    'ன':'na','னா':'naa','னி':'ni','னீ':'nii','னு':'nu','னூ':'nuu',
    'னெ':'ne','னே':'nee','னை':'nai',
    'ண':'na','ணா':'naa','ணி':'ni','ணீ':'nii','ணு':'nu','ணூ':'nuu',
    'ணெ':'ne','ணே':'nee','ணை':'nai','ணொ':'no','ணோ':'noo',
    'ய':'ya','யா':'yaa','யி':'yi','யீ':'yii','யு':'yu','யூ':'yuu',
    'யெ':'ye','யே':'yee','யை':'yai','யொ':'yo','யோ':'yoo',
    'ழ':'zh','ழா':'zhaa','ழி':'zhi','ழீ':'zhii','ழு':'zhu','ழூ':'zhuu',
    'ழெ':'zhe','ழே':'zhee','ழை':'zhai',
    'ள':'la','ளா':'laa','ளி':'li','ளீ':'lii','ளு':'lu','ளூ':'luu',
    'ளெ':'le','ளே':'lee','ளை':'lai',
    'ற':'ra','றா':'raa','றி':'ri','றீ':'rii','று':'ru','றூ':'ruu',
    'றெ':'re','றே':'ree','றை':'rai',
    'ஸ':'sa','ஸா':'saa','ஸி':'si','ஸீ':'sii','ஸு':'su','ஸூ':'suu',
    'ஷ':'sha','ஷா':'shaa','ஷி':'shi','ஷீ':'shii','ஷு':'shu','ஷூ':'shuu',
    'ஹ':'ha','ஹா':'haa','ஹி':'hi','ஹீ':'hii','ஹு':'hu','ஹூ':'huu',
    'ஜ':'ja','ஜா':'jaa','ஜி':'ji','ஜீ':'jii','ஜு':'ju','ஜூ':'juu',
    'ஜெ':'je','ஜே':'jee',
    ' ':' ',
}

def generate_tanglish_from_tamil(tamil_text):
    """Auto-generate Tanglish phonetic transliteration from Tamil text."""
    if not tamil_text:
        return ''
    result = ''
    i = 0
    text = tamil_text.strip()
    while i < len(text):
        # Try 2-char match first, then 1-char
        matched = False
        if i + 1 < len(text):
            two = text[i:i+2]
            if two in TAMIL_TANGLISH_MAP:
                result += TAMIL_TANGLISH_MAP[two]
                i += 2
                matched = True
        if not matched:
            one = text[i]
            if one in TAMIL_TANGLISH_MAP:
                result += TAMIL_TANGLISH_MAP[one]
            elif ord(one) > 127:
                result += one  # keep unknown unicode as-is
            else:
                result += one
            i += 1
    return result.strip()


# ── Nutrient normalization ────────────────────────────────────────────────────

# Standard nutrient keys for all products
STANDARD_NUTRIENT_KEYS = [
    "protein", "fat", "carbohydrates", "fiber",
    "moisture", "ash", "absorbable_protein"
]

# ALLOWED_KEYS — identical to STANDARD_NUTRIENT_KEYS, used as a whitelist filter
# at every API exit point to guarantee vitamins/minerals/carbs never reach the frontend.
ALLOWED_KEYS = set(STANDARD_NUTRIENT_KEYS)

# ── Per-product nutrition database ──────────────────────────────────────────
# LEGACY — kept for reference only. Active nutrition lookup is now handled
# entirely by nutrition_engine.py (get_nutrition_data / calculate_nutrition).
# DO NOT use PRODUCT_NUTRITION_DB in new code.
# fmt: off
_LEGACY_PRODUCT_NUTRITION_DB = {
    # ── Oil seeds ──────────────────────────────────────────────────────────────
    "almond": {
        "type": "oil", "protein": 21.2, "fat": 49.9,
        "carbohydrates": 21.6, "fiber": 12.5, "moisture": 4.5, "ash": 2.5,
    },
    "sunflower": {
        "type": "oil", "protein": 21.0, "fat": 51.0,
        "carbohydrates": 20.0, "fiber": 8.6, "moisture": 5.0, "ash": 3.0,
    },
    "groundnut": {
        "type": "oil", "protein": 25.8, "fat": 49.2,
        "carbohydrates": 16.1, "fiber": 8.5, "moisture": 5.0, "ash": 2.3,
    },
    "peanut": {
        "type": "oil", "protein": 25.8, "fat": 49.2,
        "carbohydrates": 16.1, "fiber": 8.5, "moisture": 5.0, "ash": 2.3,
    },
    "sesame": {
        "type": "oil", "protein": 17.7, "fat": 52.0,
        "carbohydrates": 23.5, "fiber": 16.9, "moisture": 5.0, "ash": 9.0,
    },
    "walnut": {
        "type": "oil", "protein": 15.2, "fat": 65.2,
        "carbohydrates": 13.7, "fiber": 6.7, "moisture": 4.0, "ash": 1.9,
    },
    "cashew": {
        "type": "oil", "protein": 18.2, "fat": 43.9,
        "carbohydrates": 30.2, "fiber": 3.3, "moisture": 5.2, "ash": 3.0,
    },
    "pistachio": {
        "type": "oil", "protein": 20.2, "fat": 45.4,
        "carbohydrates": 27.5, "fiber": 10.3, "moisture": 3.9, "ash": 3.0,
    },
    "pista": {
        "type": "oil", "protein": 20.2, "fat": 45.4,
        "carbohydrates": 27.5, "fiber": 10.3, "moisture": 3.9, "ash": 3.0,
    },
    "chia": {
        "type": "oil", "protein": 16.5, "fat": 30.7,
        "carbohydrates": 42.1, "fiber": 34.4, "moisture": 5.8, "ash": 5.6,
    },
    "flax": {
        "type": "oil", "protein": 18.3, "fat": 42.2,
        "carbohydrates": 28.9, "fiber": 27.3, "moisture": 6.5, "ash": 3.7,
    },
    "mustard": {
        "type": "oil", "protein": 26.1, "fat": 36.0,
        "carbohydrates": 28.1, "fiber": 12.2, "moisture": 6.5, "ash": 4.3,
    },
    "niger": {
        "type": "oil", "protein": 22.8, "fat": 38.5,
        "carbohydrates": 19.2, "fiber": 15.6, "moisture": 7.2, "ash": 5.1,
    },
    "pumpkin": {
        "type": "oil", "protein": 30.2, "fat": 49.1,
        "carbohydrates": 10.7, "fiber": 6.0, "moisture": 5.5, "ash": 5.4,
    },
    "cumin": {
        "type": "oil", "protein": 17.8, "fat": 22.3,
        "carbohydrates": 44.2, "fiber": 10.5, "moisture": 8.1, "ash": 7.7,
    },
    "poppy": {
        "type": "oil", "protein": 18.0, "fat": 41.6,
        "carbohydrates": 28.1, "fiber": 19.5, "moisture": 5.9, "ash": 9.1,
    },
    "soybean": {
        "type": "oil", "protein": 36.5, "fat": 19.9,
        "carbohydrates": 30.2, "fiber": 9.3, "moisture": 8.5, "ash": 4.7,
    },
    "rapeseed": {
        "type": "oil", "protein": 21.0, "fat": 46.1,
        "carbohydrates": 18.8, "fiber": 12.0, "moisture": 6.0, "ash": 4.2,
    },
    "moringa": {
        "type": "oil", "protein": 35.0, "fat": 38.0,
        "carbohydrates": 8.0, "fiber": 2.5, "moisture": 8.0, "ash": 5.0,
    },
    "cucumber": {
        "type": "oil", "protein": 24.0, "fat": 44.8,
        "carbohydrates": 11.0, "fiber": 2.5, "moisture": 7.0, "ash": 2.1,
    },
    # ── Normal grains / pulses ────────────────────────────────────────────────
    "barley": {
        "type": "normal", "protein": 12.0, "fat": 2.3,
        "carbohydrates": 73.5, "fiber": 17.0, "absorbable_protein": 7.5,
    },
    "millet": {
        "type": "normal", "protein": 11.0, "fat": 4.0,
        "carbohydrates": 67.0, "fiber": 8.5, "absorbable_protein": 6.5,
    },
    "ragi": {
        "type": "normal", "protein": 7.3, "fat": 1.3,
        "carbohydrates": 72.0, "fiber": 11.5, "absorbable_protein": 4.8,
    },
    "wheat": {
        "type": "normal", "protein": 13.2, "fat": 2.5,
        "carbohydrates": 71.2, "fiber": 10.7, "absorbable_protein": 9.5,
    },
    "rice": {
        "type": "normal", "protein": 7.5, "fat": 2.2,
        "carbohydrates": 78.2, "fiber": 3.5, "absorbable_protein": 5.5,
    },
    "oats": {
        "type": "normal", "protein": 16.9, "fat": 6.9,
        "carbohydrates": 66.3, "fiber": 10.6, "absorbable_protein": 11.0,
    },
    "corn": {
        "type": "normal", "protein": 9.4, "fat": 4.7,
        "carbohydrates": 74.3, "fiber": 2.7, "absorbable_protein": 6.0,
    },
    "sorghum": {
        "type": "normal", "protein": 10.4, "fat": 3.5,
        "carbohydrates": 72.9, "fiber": 6.3, "absorbable_protein": 7.0,
    },
    "moong": {
        "type": "normal", "protein": 23.9, "fat": 1.2,
        "carbohydrates": 59.9, "fiber": 7.6, "absorbable_protein": 17.5,
    },
    "urad": {
        "type": "normal", "protein": 25.2, "fat": 1.6,
        "carbohydrates": 59.6, "fiber": 18.3, "absorbable_protein": 17.0,
    },
    "lentil": {
        "type": "normal", "protein": 25.8, "fat": 1.1,
        "carbohydrates": 60.1, "fiber": 10.9, "absorbable_protein": 18.5,
    },
    "gram": {
        "type": "normal", "protein": 20.0, "fat": 5.0,
        "carbohydrates": 61.0, "fiber": 12.0, "absorbable_protein": 14.0,
    },
    "pea": {
        "type": "normal", "protein": 22.0, "fat": 1.5,
        "carbohydrates": 62.0, "fiber": 7.5, "absorbable_protein": 15.0,
    },
    "bean": {
        "type": "normal", "protein": 21.0, "fat": 1.5,
        "carbohydrates": 60.0, "fiber": 16.0, "absorbable_protein": 14.5,
    },
    "fenugreek": {
        "type": "normal", "protein": 23.0, "fat": 6.4,
        "carbohydrates": 58.4, "fiber": 24.6, "absorbable_protein": 15.0,
    },
    "quinoa": {
        "type": "normal", "protein": 14.1, "fat": 6.1,
        "carbohydrates": 64.2, "fiber": 7.0, "absorbable_protein": 10.5,
    },
    "amaranth": {
        "type": "normal", "protein": 13.6, "fat": 7.0,
        "carbohydrates": 65.2, "fiber": 6.7, "absorbable_protein": 9.5,
    },
    "buckwheat": {
        "type": "normal", "protein": 13.2, "fat": 3.4,
        "carbohydrates": 71.5, "fiber": 10.0, "absorbable_protein": 9.0,
    },
}
# fmt: on

# ── Class-level fallbacks — retained so normalize_nutrients / auto_classify
#    still compile; NOT used for live bill calculations anymore.
OIL_NUTRIENT_DATA = {
    "type": "oil", "protein": 20.0, "fat": 40.0,
    "carbohydrates": 25.0, "fiber": 8.0, "moisture": 6.0, "ash": 3.5,
}
NORMAL_NUTRIENT_DATA = {
    "type": "normal", "protein": 12.0, "fat": 3.5,
    "carbohydrates": 65.0, "fiber": 8.0, "absorbable_protein": 8.0,
}
OIL_NUTRIENT_DEFAULTS    = OIL_NUTRIENT_DATA   # legacy alias
NORMAL_NUTRIENT_DEFAULTS = NORMAL_NUTRIENT_DATA # legacy alias


# LEGACY — get_product_data() is superseded by nutrition_engine.get_nutrition_data().
# Kept so any stale call-site doesn't crash; redirects to new engine.

# ── Auto Tamil name translation ───────────────────────────────────────────────
ENGLISH_TO_TAMIL_MAP = {
    # ── Millets ──
    'thinai': 'தினை', 'sen thinai': 'செந்தினை', 'foxtail millet': 'தினை',
    'red foxtail millet': 'செந்தினை', 'samai': 'சாமை', 'little millet': 'சாமை',
    'kambu': 'கம்பு', 'pearl millet': 'கம்பு', 'kuthiraivali': 'குதிரைவாலி',
    'barnyard millet': 'குதிரைவாலி', 'varagu': 'வரகு', 'kodo millet': 'வரகு',
    'pani varagu': 'பனிவரகு', 'ragi': 'கேழ்வரகு', 'finger millet': 'கேழ்வரகு',
    'korali': 'கோராலி', 'sorghum': 'சோளம்', 'white sorghum': 'வெள்ளை சோளம்',
    'red sorghum': 'சிவப்பு சோளம்', 'dark sorghum': 'கருப்பு சோளம்',
    'black millet': 'கருப்பு கம்பு', 'green millet': 'பச்சை கம்பு',
    'red millet': 'சிவப்பு கம்பு', 'white millet': 'வெள்ளை கம்பு',
    'yellow millet': 'மஞ்சள் கம்பு', 'fox millet': 'நரி கம்பு',
    'browntop millet': 'ஊதா கம்பு',
    # ── Rice & Grains ──
    'rice': 'அரிசி', 'red rice': 'சிவப்பு அரிசி', 'black rice': 'கருப்பு அரிசி',
    'bamboo rice': 'மூங்கில் அரிசி', 'mappillai samba rice': 'மாப்பிள்ளை சம்பா',
    'karunguruvai black rice': 'கருங்குருவை', 'paddy': 'நெல்',
    'wheat': 'கோதுமை', 'samba wheat': 'சம்பா கோதுமை',
    'maize': 'மக்காச்சோளம்', 'corn': 'மக்காச்சோளம்', 'popcorn': 'பாப்கார்ன்',
    'sweet corn': 'இனிப்பு சோளம்',
    'oats': 'ஓட்ஸ்', 'rolled oats': 'ரோல்டு ஓட்ஸ்', 'long oats': 'நீள ஓட்ஸ்',
    'barley': 'பார்லி', 'quinoa': 'கீனோவா', 'buckwheat': 'பக்வீட்',
    'amaranth': 'அமராந்த்',
    # ── Seeds ──
    'groundnut': 'வேர்க்கடலை', 'peanut': 'வேர்க்கடலை',
    'sesame': 'எள்', 'white sesame': 'வெள்ளை எள்', 'black sesame': 'கருப்பு எள்',
    'sunflower': 'சூரியகாந்தி', 'sunflower seed': 'சூரியகாந்தி விதை',
    'mustard': 'கடுகு', 'flax': 'ஆளி விதை', 'flaxseed': 'ஆளி விதை',
    'flax seed': 'ஆளி விதை',
    'chia': 'சியா விதை', 'chia seed': 'சியா விதை',
    'pumpkin': 'பூசணி விதை', 'pumpkin seed': 'பூசணி விதை',
    'niger': 'நைஜர் விதை', 'niger seed': 'நைஜர் விதை',
    'safflower': 'குசும்பா', 'watermelon': 'தர்பூசணி',
    'watermelon seed': 'தர்பூசணி விதை',
    'poppy seeds': 'கசகசா', 'poppy seed': 'கசகசா',
    'cucumber seed': 'வெள்ளரி விதை',
    'hemp': 'ஹெம்ப்', 'hemp seed': 'ஹெம்ப் விதை',
    'carom seed': 'ஓமம்', 'dill seed': 'சதகுப்பை விதை',
    'canary seed': 'கேனரி விதை',
    # ── Nuts ──
    'almond': 'பாதாம்', 'cashew': 'முந்திரி', 'walnut': 'அக்ரோட்',
    'pistachio': 'பிஸ்தா', 'charoli': 'சாரோலி',
    # ── Pulses / Legumes ──
    'green gram': 'பயத்தம்', 'moong': 'பயத்தம்',
    'black gram': 'உளுந்து', 'urad': 'உளுந்து', 'black urad': 'கருப்பு உளுந்து',
    'chickpea': 'கொண்டை கடலை', 'chickpeas': 'கொண்டை கடலை',
    'horse gram': 'கொள்ளு', 'kollu': 'கொள்ளு',
    'lentil': 'பருப்பு', 'red lentil': 'மைசூர் பருப்பு',
    'red toor dal': 'சிவப்பு துவரை', 'toor dal': 'துவரை',
    'soya': 'சோயா', 'soybean': 'சோயா',
    'rajma': 'ராஜ்மா', 'kidney beans': 'ராஜ்மா',
    'beans': 'அவரை', 'field beans': 'அவரைக்காய்',
    'green peas': 'பச்சை பட்டாணி',
    'red cow pea': 'சிவப்பு தட்டபயிர்',
    'black eyed peas': 'கராமணி',
    # ── Spices & Condiments ──
    'turmeric': 'மஞ்சள்', 'kasturi turmeric': 'கஸ்தூரி மஞ்சள்',
    'pepper': 'மிளகு', 'black pepper': 'மிளகு',
    'cumin': 'சீரகம்', 'black cumin': 'கருஞ்சீரகம்',
    'fenugreek': 'வெந்தயம்', 'coriander': 'கொத்தமல்லி',
    'cardamom': 'ஏலக்காய்', 'cloves': 'கிராம்பு',
    'cinnamon': 'பட்டை', 'ginger': 'இஞ்சி', 'garlic': 'பூண்டு',
    'onion': 'வெங்காயம்', 'salt': 'உப்பு', 'bamboo salt': 'மூங்கில் உப்பு',
    'sugar': 'சர்க்கரை',
    # ── Sweeteners & Dry fruits ──
    'jaggery': 'வெல்லம்', 'honey': 'தேன்',
    'dates': 'பேரீச்சை', 'black dates': 'கருப்பு பேரீச்சை',
    'raisin': 'திராட்சை', 'raisins': 'திராட்சை',
    'coconut': 'தேங்காய்',
    # ── Herbal / Special ──
    'moringa': 'முருங்கை', 'moringa seed': 'முருங்கை விதை',
    'ashwagandha': 'அமுக்கிரா', 'alfalfa': 'குதிரைமசால்',
    'costus root': 'கொஸ்டஸ் வேர்', 'asparagus': 'சதாவரி',
    'blue lotus': 'நீலதாமரை', 'milk thistle': 'பால் நெருஞ்சி',
    'banyan seed': 'ஆல விதை', 'betel nut': 'பாக்கு',
    'lime stone': 'சுண்ணாம்பு', 'egg shell': 'முட்டை ஓடு',
    'olive': 'ஆலிவ்',
    # ── Oil ──
    'oil': 'எண்ணெய்', 'groundnut oil': 'வேர்க்கடலை எண்ணெய்',
    'sesame oil': 'நல்லெண்ணெய்', 'coconut oil': 'தேங்காய் எண்ணெய்',
    # ── Feed ──
    'bird feed': 'பறவை தீவனம்', 'hamster pellet': 'ஹம்ஸ்டர் பெல்லெட்',
    'rabbit pellet': 'முயல் பெல்லெட்',
}

def clean_name(name):
    """Normalize product name for matching."""
    return name.lower().strip().replace('-', ' ').replace('_', ' ')


def translate_to_tamil(name):
    """
    3-layer Tamil name generation:
      1. Dictionary exact match
      2. Dictionary partial match (longest key first)
      3. DB reuse — look up existing product with same English name
    Falls back to original name if no match found.
    """
    if not name:
        return ''
    key = clean_name(name)

    # Layer 1: Exact match
    if key in ENGLISH_TO_TAMIL_MAP:
        return ENGLISH_TO_TAMIL_MAP[key]

    # Layer 2: Partial match (longest key first to avoid short-key collisions)
    for eng, tam in sorted(ENGLISH_TO_TAMIL_MAP.items(), key=lambda x: -len(x[0])):
        if eng in key:
            return tam

    # Layer 3: Reuse existing Tamil name from DB for same English name
    try:
        conn = get_db()
        row = conn.execute(
            'SELECT name_tamil FROM products WHERE LOWER(TRIM(name_english))=? AND name_tamil IS NOT NULL AND name_tamil != \"\" LIMIT 1',
            (key,)
        ).fetchone()
        conn.close()
        if row and row['name_tamil']:
            return row['name_tamil']
    except Exception:
        pass

    return name  # fallback = same name

def get_product_data(name):
    """LEGACY shim → delegates to nutrition_engine.get_nutrition_data()."""
    return get_nutrition_data(name)


# ─────────────────────────────────────────────────────────────────────────────
# LEGACY NUTRITION HELPERS — superseded by nutrition_engine.py in v20.
#
# calculate_item_nutrition() and calculate_nutrition_summary() are NO LONGER
# called by the bill route.  They are retained here (as dead code) only so
# that any external script or test that imported them directly does not raise
# an ImportError.  They will be removed in a future cleanup pass.
# ─────────────────────────────────────────────────────────────────────────────

def calculate_item_nutrition(weight_g, _nutrients_ignored, nutrient_type,
                              name_english=''):
    """
    LEGACY — no longer called by /bill route (v20+).
    Delegates to nutrition_engine.calculate_nutrition() for backward compat.
    """
    result = {k: 0.0 for k in STANDARD_NUTRIENT_KEYS}
    if weight_g <= 0:
        return result
    eng_result = _calc_nutrition_engine(
        name=name_english, category='', quantity_g=weight_g
    )
    for k in STANDARD_NUTRIENT_KEYS:
        result[k] = eng_result['nutrition'].get(k, 0.0)
    return result


def calculate_nutrition_summary(items):
    """
    Calculates weighted-average per-100g nutrition for a set of bill items.

    The engine's calculate_nutrition() returns grams scaled to purchase weight.
    This function converts those gram totals BACK to per-100g percentages by
    dividing by (total_weight_g / 100), so the bill template always displays
    values in the range 0–100, never large gram numbers like 7527.

    Args:
        items (list): [{"product": {"nutrient_type": str,
                                    "name_english": str, ...},
                        "weight": float (grams)}, ...]

    Returns:
        {
          "totals":     {nutrient: percent_value, ...},  # per-100g %
          "has_oil":    bool,
          "has_normal": bool,
          "oil":        {nutrient: percent_value, ...},
          "normal":     {nutrient: percent_value, ...},
        }
    """
    _zero      = lambda: {k: 0.0 for k in STANDARD_NUTRIENT_KEYS}
    oil_acc    = _zero()
    normal_acc = _zero()
    has_oil    = False
    has_normal = False
    # Track total weight per category to convert grams → percentages correctly
    oil_weight_g    = 0.0
    normal_weight_g = 0.0

    for entry in items:
        product = entry.get('product', {})
        weight  = float(entry.get('weight', 0))
        if weight <= 0:
            continue
        name    = (product.get('name_english') or
                   product.get('name') or
                   product.get('id', '?'))
        cat     = product.get('category', '')

        eng = _calc_nutrition_engine(name=name, category=cat, quantity_g=weight)
        nutr      = eng.get('nutrition', {})
        item_type = eng.get('type', 'normal')

        if item_type in ('oil',):
            has_oil = True
            oil_weight_g += weight
            for k in STANDARD_NUTRIENT_KEYS:
                oil_acc[k] += nutr.get(k, 0.0)
        elif item_type not in ('feed', 'other'):
            has_normal = True
            normal_weight_g += weight
            for k in STANDARD_NUTRIENT_KEYS:
                normal_acc[k] += nutr.get(k, 0.0)

    # ── Convert gram totals → per-100g percentages ────────────────────────────
    # gram_total / (weight_g / 100)  =  (gram_total * 100) / weight_g
    # This gives the same value as the original per-100g DB entry.
    def _grams_to_pct(acc, weight_g):
        if weight_g <= 0:
            return {k: 0.0 for k in STANDARD_NUTRIENT_KEYS}
        return {k: round((acc[k] * 100.0) / weight_g, 1) for k in STANDARD_NUTRIENT_KEYS}

    oil_pct    = _grams_to_pct(oil_acc,    oil_weight_g)
    normal_pct = _grams_to_pct(normal_acc, normal_weight_g)

    # Merged totals: weighted average across ALL items (oil + normal combined)
    total_weight_g = oil_weight_g + normal_weight_g
    if total_weight_g > 0:
        merged_acc = {k: oil_acc[k] + normal_acc[k] for k in STANDARD_NUTRIENT_KEYS}
        merged_pct = {k: round((merged_acc[k] * 100.0) / total_weight_g, 1)
                      for k in STANDARD_NUTRIENT_KEYS if k in ALLOWED_KEYS}
        # ── ASH FIX ──────────────────────────────────────────────────────────
        # Normal-type items never populate ash (it's in their hide list), so
        # dividing oil_acc["ash"] by total_weight_g (oil + normal) under-reports
        # ash when the package is mixed.  When oil seeds are present, use the
        # oil-only weighted average so the displayed value matches the oil-seed
        # ash content — consistent with moisture which behaves the same way.
        if has_oil and oil_weight_g > 0:
            merged_pct['ash'] = oil_pct['ash']   # already per-100g of oil portion
    else:
        merged_pct = {k: 0.0 for k in STANDARD_NUTRIENT_KEYS if k in ALLOWED_KEYS}


    return {
        'totals':     merged_pct,
        'has_oil':    has_oil,
        'has_normal': has_normal,
        'oil':        {k: oil_pct[k]    for k in STANDARD_NUTRIENT_KEYS if k in ALLOWED_KEYS},
        'normal':     {k: normal_pct[k] for k in STANDARD_NUTRIENT_KEYS if k in ALLOWED_KEYS},
    }


def normalize_nutrients(raw_data, nutrient_type='normal'):
    """
    Convert ANY nutrient dict to the single standard 7-key structure.

    Allowed output keys ONLY:
        protein, fat, carbohydrates, fiber, moisture, ash, absorbable_protein

    ALL other keys (vitamins, minerals, carbs, crude_*, h2o, etc.) are dropped.
    Missing values default to 0.0 so arithmetic always works downstream.

    nutrient_type='normal':
        protein / crude protein     -> protein
        fat                         -> fat
        carbs / carbohydrates       -> carbohydrates
        fiber / fibre               -> fiber
        moisture                    -> moisture
        ash                         -> ash
        absorbable_protein          -> absorbable_protein

    nutrient_type='oil':
        crude protein / protein     -> protein
        crude fat / fat             -> fat
        crude fibre / crude fiber / fiber / fibre -> fiber
        moisture (h2o) / moisture / h2o -> moisture
        crude ash / ash             -> ash
        carbohydrates / carbs       -> carbohydrates
        absorbable_protein          -> absorbable_protein
    """
    if not raw_data or not isinstance(raw_data, dict):
        return {k: 0.0 for k in STANDARD_NUTRIENT_KEYS}

    lc = {k.lower().strip(): v for k, v in raw_data.items()}

    def _get(*candidates):
        for c in candidates:
            if c in lc and lc[c] is not None:
                try:
                    return float(lc[c])
                except (TypeError, ValueError):
                    pass
        return 0.0

    result = {k: 0.0 for k in STANDARD_NUTRIENT_KEYS}

    if nutrient_type == 'oil':
        result['protein']            = _get('crude protein', 'protein')
        result['fat']                = _get('crude fat', 'fat')
        result['fiber']              = _get('crude fibre', 'crude fiber', 'fiber', 'fibre')
        result['moisture']           = _get('moisture (h2o)', 'moisture', 'h2o')
        result['ash']                = _get('crude ash', 'ash')
        result['carbohydrates']      = _get('carbohydrates', 'carbs')
        result['absorbable_protein'] = _get('absorbable_protein', 'absorbable protein')
    else:
        # 'normal' — or any unrecognised type falls here safely
        result['protein']            = _get('protein', 'crude protein')
        result['fat']                = _get('fat', 'crude fat')
        result['carbohydrates']      = _get('carbohydrates', 'carbs')
        result['fiber']              = _get('fiber', 'fibre', 'crude fibre', 'crude fiber')
        result['moisture']           = _get('moisture', 'moisture (h2o)')
        result['ash']                = _get('ash', 'crude ash')
        result['absorbable_protein'] = _get('absorbable_protein', 'absorbable protein')

    # Guarantee: only the 7 standard keys, no extras, all numeric
    return {k: result[k] for k in STANDARD_NUTRIENT_KEYS}


def classify_nutrient_type(nutrients_dict):
    """
    Strict 3-rule classification — no guessing.

    Step 0: normalise keys in a local copy so all three rules work on
            clean key names regardless of the input format.
            Removes vitamins / minerals before evaluation.

    Rule 1 (fat threshold):
        fat >= 15  →  "oil"

    Rule 2 (oil-specific keys present):
        moisture > 0  OR  ash > 0  →  force "oil"

    Rule 3 (plain 4-key profile):
        ONLY {protein, fat, carbohydrates, fiber} present (no moisture/ash)
        →  force "normal"

    Fallback: "normal"
    """
    if not isinstance(nutrients_dict, dict):
        return 'normal'

    # ── Step 0: normalise keys locally ───────────────────────────────────────
    KEY_MAP = {
        'carbs':          'carbohydrates',
        'h2o':            'moisture',
        'moisture (h2o)': 'moisture',
        'crude protein':  'protein',
        'crude fat':      'fat',
        'crude fibre':    'fiber',
        'crude fiber':    'fiber',
        'crude ash':      'ash',
        'fibre':          'fiber',
    }
    DROP_KEYS = {'vitamins', 'minerals'}

    lc = {}
    for k, v in nutrients_dict.items():
        k_low = k.lower().strip()
        if k_low in DROP_KEYS:
            continue
        mapped = KEY_MAP.get(k_low, k_low)
        if v is not None:
            try:
                lc[mapped] = float(v)
            except (TypeError, ValueError):
                lc[mapped] = 0.0

    def _val(key):
        return lc.get(key, 0.0) or 0.0

    fat = _val('fat')

    # Rule 1 ──────────────────────────────────────────────────────────────────
    if fat >= 15.0:
        return 'oil'

    # Rule 2 ──────────────────────────────────────────────────────────────────
    if _val('moisture') > 0.0 or _val('ash') > 0.0:
        return 'oil'

    # Rule 3 ──────────────────────────────────────────────────────────────────
    present = {k for k, v in lc.items() if v > 0.0}
    plain_4 = {'protein', 'fat', 'carbohydrates', 'fiber'}
    if present and present.issubset(plain_4):
        return 'normal'

    return 'normal'


def is_normalized(nutrients_dict):
    """
    Return True only if nutrients_dict contains EXACTLY the 7 standard keys
    and no legacy keys whatsoever.
    """
    if not isinstance(nutrients_dict, dict):
        return False
    keys = set(nutrients_dict.keys())
    standard = set(STANDARD_NUTRIENT_KEYS)
    legacy = {'carbs', 'vitamins', 'minerals', 'crude protein', 'crude fat',
              'crude fibre', 'crude fiber', 'crude ash', 'h2o', 'moisture (h2o)',
              'fibre', 'absorbable protein'}
    # Must have all 7 standard keys, no legacy keys, no unknown extras
    return keys == standard and not (keys & legacy)


# ── Package definitions ───────────────────────────────────────────────────────
# nutrients_per_100g uses the SAME standard 7-key structure as products.
# Legacy keys (carbs, vitamins, minerals) have been replaced.
PACKAGES = [
    {
        "id": "pkg1",
        "name_english": "Package 1",
        "name_tamil": "பேக்கேஜ் 1",
        "name_tanglish": "paakkeej 1",
        "emoji": "📦",
        "category": "Packages",
        "price_per_kg": 100,
        "is_package": True,
        "items": [
            {"name_tamil":"S.மக்கா","name_english":"S.Makka","ratio_kg":12,"price_per_kg":80,"nutrients_per_100g":{"protein":9.4,"fat":4.7,"fiber":2.7,"carbohydrates":74.3,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"மூக்கடல","name_english":"Mukkadala","ratio_kg":5,"price_per_kg":90,"nutrients_per_100g":{"protein":22.0,"fat":1.2,"fiber":8.0,"carbohydrates":60.0,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"வெ.பட்டானி","name_english":"Ve.Pattani","ratio_kg":4,"price_per_kg":95,"nutrients_per_100g":{"protein":20.0,"fat":1.0,"fiber":7.5,"carbohydrates":62.0,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"ப.பட்டானி","name_english":"Pa.Pattani","ratio_kg":1,"price_per_kg":95,"nutrients_per_100g":{"protein":21.0,"fat":1.1,"fiber":7.0,"carbohydrates":61.0,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"சப்போலா","name_english":"Sappola","ratio_kg":5,"price_per_kg":100,"nutrients_per_100g":{"protein":10.0,"fat":3.5,"fiber":5.0,"carbohydrates":65.0,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"கோதுமை","name_english":"Wheat","ratio_kg":5,"price_per_kg":85,"nutrients_per_100g":{"protein":13.2,"fat":2.5,"fiber":10.7,"carbohydrates":71.2,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"சி.அரிசி","name_english":"Si.Rice","ratio_kg":3,"price_per_kg":90,"nutrients_per_100g":{"protein":7.5,"fat":2.2,"fiber":3.5,"carbohydrates":78.2,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"வெ.சோலம்","name_english":"Ve.Solam","ratio_kg":3,"price_per_kg":80,"nutrients_per_100g":{"protein":10.4,"fat":3.5,"fiber":6.3,"carbohydrates":72.9,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"சி.சோலம்","name_english":"Si.Solam","ratio_kg":2,"price_per_kg":80,"nutrients_per_100g":{"protein":10.4,"fat":3.5,"fiber":6.3,"carbohydrates":72.9,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"கம்பு","name_english":"Pearl Millet","ratio_kg":2,"price_per_kg":85,"nutrients_per_100g":{"protein":11.0,"fat":5.0,"fiber":8.5,"carbohydrates":67.0,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"ப.பயிர்","name_english":"Pa.Payir","ratio_kg":3,"price_per_kg":100,"nutrients_per_100g":{"protein":23.9,"fat":1.2,"fiber":7.6,"carbohydrates":59.9,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"ந.காரமனி","name_english":"Na.Karamani","ratio_kg":2,"price_per_kg":100,"nutrients_per_100g":{"protein":23.5,"fat":0.5,"fiber":4.5,"carbohydrates":60.7,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"க.உலுந்து","name_english":"Ka.Ulundhu","ratio_kg":1,"price_per_kg":120,"nutrients_per_100g":{"protein":25.2,"fat":1.6,"fiber":18.3,"carbohydrates":59.6,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"வேற்கடல","name_english":"Groundnut","ratio_kg":1.5,"price_per_kg":130,"nutrients_per_100g":{"protein":25.8,"fat":49.2,"fiber":8.5,"carbohydrates":16.1,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"சூரியகாந்தி","name_english":"Sunflower","ratio_kg":1.5,"price_per_kg":130,"nutrients_per_100g":{"protein":20.8,"fat":51.5,"fiber":8.6,"carbohydrates":20.0,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"சோயா","name_english":"Soya","ratio_kg":2,"price_per_kg":110,"nutrients_per_100g":{"protein":36.5,"fat":19.9,"fiber":9.3,"carbohydrates":30.2,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"நரிபயிர்","name_english":"Nari Payir","ratio_kg":2,"price_per_kg":100,"nutrients_per_100g":{"protein":22.0,"fat":1.5,"fiber":10.0,"carbohydrates":58.0,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
        ]
    },
    {
        "id": "pkg2",
        "name_english": "Package 2",
        "name_tamil": "பேக்கேஜ் 2",
        "name_tanglish": "paakkeej 2",
        "emoji": "📦",
        "category": "Packages",
        "price_per_kg": 100,
        "is_package": True,
        "items": [
            {"name_tamil":"கம்பு","name_english":"Pearl Millet","ratio_kg":15,"price_per_kg":85,"nutrients_per_100g":{"protein":11.0,"fat":5.0,"fiber":8.5,"carbohydrates":67.0,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"கேழ்வரகு","name_english":"Ragi","ratio_kg":7.5,"price_per_kg":90,"nutrients_per_100g":{"protein":7.3,"fat":1.3,"fiber":11.5,"carbohydrates":72.0,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"உடச்ச மக்கா","name_english":"Udacha Makka","ratio_kg":7,"price_per_kg":80,"nutrients_per_100g":{"protein":9.4,"fat":4.7,"fiber":2.7,"carbohydrates":74.3,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"கோதுமை","name_english":"Wheat","ratio_kg":6,"price_per_kg":85,"nutrients_per_100g":{"protein":13.2,"fat":2.5,"fiber":10.7,"carbohydrates":71.2,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"சி.அரிசி","name_english":"Si.Rice","ratio_kg":3,"price_per_kg":90,"nutrients_per_100g":{"protein":7.5,"fat":2.2,"fiber":3.5,"carbohydrates":78.2,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"வெ.சோலம்","name_english":"Ve.Solam","ratio_kg":5,"price_per_kg":80,"nutrients_per_100g":{"protein":10.4,"fat":3.5,"fiber":6.3,"carbohydrates":72.9,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"சி.சோலம்","name_english":"Si.Solam","ratio_kg":2,"price_per_kg":80,"nutrients_per_100g":{"protein":10.4,"fat":3.5,"fiber":6.3,"carbohydrates":72.9,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"ப.பயிர்","name_english":"Pa.Payir","ratio_kg":2,"price_per_kg":100,"nutrients_per_100g":{"protein":23.9,"fat":1.2,"fiber":7.6,"carbohydrates":59.9,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"க.உலுந்து","name_english":"Ka.Ulundhu","ratio_kg":1,"price_per_kg":120,"nutrients_per_100g":{"protein":25.2,"fat":1.6,"fiber":18.3,"carbohydrates":59.6,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
            {"name_tamil":"மூக்கடல","name_english":"Mukkadala","ratio_kg":1.5,"price_per_kg":90,"nutrients_per_100g":{"protein":22.0,"fat":1.2,"fiber":8.0,"carbohydrates":60.0,"moisture":0.0,"ash":0.0,"absorbable_protein":0.0}},
        ]
    }
]


def _calc_package_nutrients(pkg_id, qty_kg, active_item_names):
    """
    LEGACY — kept for backward compat. New code uses calculate_package_nutrition().
    """
    pkg = next((p for p in PACKAGES if p['id'] == pkg_id), None)
    if not pkg:
        return None
    active = [it for it in pkg['items'] if it['name_tamil'] in active_item_names]
    if not active:
        return None
    total_ratio = sum(it['ratio_kg'] for it in active)
    if total_ratio == 0:
        return None
    totals = {k: 0.0 for k in STANDARD_NUTRIENT_KEYS}
    for it in active:
        item_kg = (it['ratio_kg'] / total_ratio) * qty_kg
        units   = item_kg * 10
        raw     = it.get('nutrients_per_100g', {})
        nuts    = normalize_nutrients(raw, classify_nutrient_type(raw))
        for key in totals:
            totals[key] = round(totals[key] + (nuts.get(key) or 0.0) * units, 4)
    return {k: round(v, 1) for k, v in totals.items()}


# ── OIL SEED DETECTION — used by calculate_package_nutrition ─────────────────
# Ingredient is classified as "oil seed" if its name matches any keyword below.
_PKG_OIL_KEYWORDS = (
    "groundnut", "peanut", "sunflower", "sesame", "flax", "niger",
    "mustard", "chia", "rapeseed", "canola", "safflower", "sappola",
    "sapola", "walnut", "cashew", "almond", "pista", "pistachio",
    "soybean", "soya", "poppy", "pumpkin seed",
)

def _is_oil_ingredient(name: str) -> bool:
    """Return True if ingredient name matches any known oil-seed keyword."""
    name_lc = (name or '').lower()
    return any(kw in name_lc for kw in _PKG_OIL_KEYWORDS)


def calculate_package_nutrition(package_items: list) -> dict:
    """
    Calculate WEIGHTED-AVERAGE per-100g nutrition for a package.

    This is the AUTHORITATIVE function for all package nutrition calculations.
    It must be used both for the API response and the bill template display.

    Algorithm:
      1. total_weight_kg = Σ item["weight_kg"]
      2. For each nutrient key:
             weighted_value = Σ (item_weight_kg / total_weight_kg) * nutrient_per_100g
         Result is already a percentage (per-100g value), valid 0–100.
      3. Detect if ANY ingredient is an oil seed → sets has_oil flag.

    Args:
        package_items: list of dicts, each with:
            {
              "name_english": str,          # used for oil-seed detection
              "name_tamil":   str,          # optional, for display
              "weight_kg":    float,        # actual kg in the package
              "nutrients_per_100g": {       # USDA/IFCT per-100g values
                  "protein": float,
                  "fat": float,
                  "carbohydrates": float,
                  "fiber": float,
                  "moisture": float,        # 0 for normal grains
                  "ash": float,             # 0 for normal grains
                  "absorbable_protein": float,
              }
            }

    Returns:
        {
            "nutrition":  {key: float, ...},   # weighted-average % per 100g
            "has_oil":    bool,                 # True if any oil seed present
            "has_normal": bool,                 # True if any normal grain present
            "total_weight_kg": float,
        }
    """
    if not package_items:
        return {
            "nutrition":       {k: 0.0 for k in STANDARD_NUTRIENT_KEYS},
            "has_oil":         False,
            "has_normal":      False,
            "total_weight_kg": 0.0,
        }

    # Sum actual weights (may differ from ratio_kg when user edits quantities)
    total_weight_kg = sum(float(it.get('weight_kg', 0)) for it in package_items)
    if total_weight_kg <= 0:
        return {
            "nutrition":       {k: 0.0 for k in STANDARD_NUTRIENT_KEYS},
            "has_oil":         False,
            "has_normal":      False,
            "total_weight_kg": 0.0,
        }

    # Weighted accumulator — values are per-100g percentages
    weighted = {k: 0.0 for k in STANDARD_NUTRIENT_KEYS}
    has_oil    = False
    has_normal = False

    for it in package_items:
        w_kg   = float(it.get('weight_kg', 0))
        if w_kg <= 0:
            continue
        weight_fraction = w_kg / total_weight_kg   # 0.0–1.0

        # Resolve per-100g nutrition from item dict
        raw = it.get('nutrients_per_100g', {})

        # Supplement missing values using nutrition_engine DB lookup
        name_en = (it.get('name_english') or it.get('name_en') or '').strip()
        if name_en:
            db_entry = get_nutrition_data(name_en)
            if db_entry:
                # Merge: DB wins over zero-filled placeholders in package defs
                merged = {}
                for k in STANDARD_NUTRIENT_KEYS:
                    db_val  = float(db_entry.get(k) or 0.0)
                    raw_val = float(raw.get(k) or 0.0)
                    # Use DB value if raw is 0 (unfilled placeholder)
                    merged[k] = db_val if raw_val == 0.0 and db_val > 0.0 else raw_val
                raw = merged

        ntype = classify_nutrient_type(raw)
        nuts  = normalize_nutrients(raw, ntype)

        # Classify ingredient
        if _is_oil_ingredient(name_en):
            has_oil = True
        else:
            has_normal = True

        # Accumulate weighted-average nutrients
        for k in STANDARD_NUTRIENT_KEYS:
            weighted[k] += weight_fraction * float(nuts.get(k) or 0.0)

    # Round to 1 decimal — these are already per-100g percentage values
    nutrition = {k: round(weighted[k], 1) for k in STANDARD_NUTRIENT_KEYS}


    return {
        "nutrition":       nutrition,
        "has_oil":         has_oil,
        "has_normal":      has_normal,
        "total_weight_kg": round(total_weight_kg, 3),
    }


def auto_classify_products(conn):
    """
    Classifies every product as "normal", "oil", or "other" using a 4-step
    priority system. Called on every startup.
    Does NOT touch IDs, names, prices, or any other column.

    Priority order (first matching rule wins):

      Step 1 — FAT RULE (highest priority):
          fat >= 15  →  "oil"

      Step 2 — STRICT OIL KEYWORDS:
          name contains any of:
              groundnut, peanut, almond, cashew, walnut, pista, sesame,
              flax, sunflower, chia, mustard, rapeseed, niger, pumpkin,
              cumin, poppy, soybean
          (generic "seed", "nut", "oil" intentionally excluded)
          →  "oil"

      Step 3 — NORMAL GRAIN KEYWORDS:
          name contains any of:
              rice, millet, wheat, dal, bean, pea, gram, corn, oats
          →  "normal"

      Step 4 — OTHER CATEGORY:
          name contains any of:
              root, bark, leaf, flower, powder, gum, mix, feed, pellet,
              salt, candy, herb
          →  "other"

      Fallback: "normal"
    """
    # ── Key normalisation map ─────────────────────────────────────────────────
    KEY_MAP = {
        'carbs':          'carbohydrates',
        'h2o':            'moisture',
        'moisture (h2o)': 'moisture',
        'crude protein':  'protein',
        'crude fat':      'fat',
        'crude fibre':    'fiber',
        'crude fiber':    'fiber',
        'crude ash':      'ash',
        'fibre':          'fiber',
    }
    DROP_KEYS = {'vitamins', 'minerals'}

    # ── Classification keyword lists ──────────────────────────────────────────
    # Step 2: strict oil-producing seeds/nuts — "seed", "nut", "oil" removed
    OIL_KEYWORDS = [
        'groundnut', 'peanut', 'almond', 'cashew', 'walnut', 'pista',
        'sesame', 'flax', 'sunflower', 'chia', 'mustard', 'rapeseed',
        'niger', 'pumpkin', 'cumin', 'poppy', 'soybean',
    ]
    # Step 3: cereal/pulse/grain crops
    NORMAL_KEYWORDS = [
        'rice', 'millet', 'wheat', 'dal', 'bean', 'pea', 'gram', 'corn', 'oats',
    ]
    # Step 4: non-food / medicinal / processed products
    OTHER_KEYWORDS = [
        'root', 'bark', 'leaf', 'flower', 'powder', 'gum', 'mix',
        'feed', 'pellet', 'salt', 'candy', 'herb',
    ]

    rows = conn.execute(
        'SELECT id, name_english, nutrients FROM products'
    ).fetchall()

    for row in rows:
        pid     = row[0]
        name    = (row[1] or row[0] or '').strip()
        raw_str = row[2] or '{}'

        # ── Load and normalise nutrient JSON ──────────────────────────────────
        try:
            raw = json.loads(raw_str) if isinstance(raw_str, str) else (raw_str or {})
            if not isinstance(raw, dict):
                raw = {}
        except Exception:
            raw = {}

        cleaned = {}
        for k, v in raw.items():
            k_low = k.lower().strip()
            if k_low in DROP_KEYS:
                continue
            mapped = KEY_MAP.get(k_low, k_low)
            if v is not None:
                try:
                    cleaned[mapped] = float(v)
                except (TypeError, ValueError):
                    cleaned[mapped] = 0.0

        # Fill all 7 standard keys; strip non-standard extras
        for k in STANDARD_NUTRIENT_KEYS:
            cleaned.setdefault(k, 0.0)
        cleaned = {k: v for k, v in cleaned.items() if k in ALLOWED_KEYS}

        # ── 4-step priority classification ────────────────────────────────────────────
        fat     = cleaned.get('fat', 0) or 0.0
        name_lc = name.lower()
        rule    = None

        # ── HARD OVERRIDE (failsafe): sunflower is always oil ─────────────────
        # Applied before all other rules to prevent 'Sunflower Seeds' ever
        # being classified as normal — the most common misclassification observed.
        if 'sunflower' in name_lc:
            ntype = 'oil'
            rule  = 'FORCED-OIL(sunflower)'

        # Step 1: fat threshold — always wins regardless of name
        elif fat >= 15.0:
            ntype = 'oil'
            rule  = f'Step1-fat({fat})'

        # Step 2: strict oil-crop keywords (fat missing or < 15)
        elif any(kw in name_lc for kw in OIL_KEYWORDS):
            ntype = 'oil'
            rule  = f'Step2-oil-kw(fat={fat})'

        # Step 3: cereal/pulse/grain keywords
        elif any(kw in name_lc for kw in NORMAL_KEYWORDS):
            ntype = 'normal'
            rule  = f'Step3-normal-kw(fat={fat})'

        # Step 4: other / medicinal / processed
        elif any(kw in name_lc for kw in OTHER_KEYWORDS):
            ntype = 'other'
            rule  = f'Step4-other-kw(fat={fat})'

        # Fallback
        else:
            ntype = 'normal'
            rule  = f'Fallback(fat={fat})'

        # ── Write back to DB ──────────────────────────────────────────────────
        conn.execute(
            'UPDATE products SET nutrient_type=?, nutrients=? WHERE id=?',
            (ntype, json.dumps(cleaned), pid)
        )

        # ── Debug print ───────────────────────────────────────────────────────

    conn.commit()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if getattr(sys, 'frozen', False):
    template_dir = os.path.join(sys._MEIPASS, 'backend', 'templates')
    static_dir   = os.path.join(sys._MEIPASS, 'backend', 'static')
else:
    template_dir = os.path.join(BASE_DIR, 'templates')
    static_dir   = os.path.join(BASE_DIR, 'static')

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

# ── Startup diagnostics (always printed — confirms correct instance is running) ──
print("=" * 60)
print("RUNNING FILE  :", __file__)
print("CWD           :", os.getcwd())
print("ROOT          :", app.root_path)
print("TEMPLATE DIR  :", app.template_folder)
_thermal_path = os.path.join(app.template_folder, "thermal_preview.html")
print("THERMAL EXISTS:", os.path.exists(_thermal_path))
print("THERMAL PATH  :", _thermal_path)
try:
    with app.app_context():
        _tmpl_list = app.jinja_env.list_templates()
    print("ALL TEMPLATES :", _tmpl_list)
except Exception as _e:
    print("ALL TEMPLATES : (could not list —", _e, ")")
print("=" * 60)

# Fix 3: Ensure Tamil Unicode is never escaped in JSON responses
app.config['JSON_AS_ASCII'] = False
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024   # image uploads are base64 JSON
# The UI is served from this same origin; only allow local origins cross-origin.
CORS(app, origins=[r'http://127\.0\.0\.1(:\d+)?', r'http://localhost(:\d+)?'])

# ── App Password / Login System ───────────────────────────────────────────────
# Use a stable secret key so sessions survive server restarts.
# Falls back to a random key if FLASK_SECRET is not set in environment.
_secret_key_file = data_path(".secret_key")
try:
    if os.path.exists(_secret_key_file):
        with open(_secret_key_file, "rb") as _f:
            app.secret_key = _f.read()
    else:
        _key = os.urandom(32)
        with open(_secret_key_file, "wb") as _f:
            _f.write(_key)
        app.secret_key = _key
except Exception:
    app.secret_key = os.environ.get("FLASK_SECRET", "dhana-dhanya-kadai-secret-2024")
from datetime import timedelta
app.permanent_session_lifetime = timedelta(hours=12)

DEFAULT_PASSWORD = '1'

def _hash_pw(pw):
    return sha256(pw.encode()).hexdigest()

def _init_app_settings():
    """Ensure app_settings table exists with default password + bill counter seed."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    existing = conn.execute("SELECT value FROM app_settings WHERE key='app_password_hash'").fetchone()
    if not existing:
        conn.execute("INSERT INTO app_settings (key, value) VALUES ('app_password_hash', ?)", (_hash_pw(DEFAULT_PASSWORD),))
    # ISSUE 5: Seed bill number settings if not already set
    bill_start = conn.execute("SELECT value FROM app_settings WHERE key=?", (BILL_NUMBER_START_KEY,)).fetchone()
    if not bill_start:
        conn.execute("INSERT INTO app_settings (key, value) VALUES (?, '1')", (BILL_NUMBER_START_KEY,))
    bill_next = conn.execute("SELECT value FROM app_settings WHERE key=?", (BILL_NUMBER_NEXT_KEY,)).fetchone()
    if not bill_next:
        seed_next = (bill_start['value'] if bill_start else '1')
        conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (BILL_NUMBER_NEXT_KEY, seed_next))
    conn.commit()
    conn.close()
    try:
        from services.printer_manager import PrinterManager
        PrinterManager(get_db).ensure_defaults()
    except Exception:
        pass

def _check_password(pw):
    conn = get_db()
    row = conn.execute("SELECT value FROM app_settings WHERE key='app_password_hash'").fetchone()
    conn.close()
    if not row:
        return pw == DEFAULT_PASSWORD
    return row['value'] == _hash_pw(pw)

def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

# ── Login/Logout routes ──────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login_route():
    # If already logged in, redirect to dashboard
    if request.method == 'GET' and session.get('logged_in'):
        return redirect('/')
    if request.method == 'POST':
        data = request.get_json() if request.is_json else {'password': request.form.get('password', '')}
        pw = data.get('password', '') if data else ''
        if _check_password(pw):
            session.permanent = True
            session['logged_in'] = True
            return jsonify({'status': 'ok', 'redirect': '/'})
        return jsonify({'error': 'Wrong password'}), 403
    # GET — serve login page
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — DHANA DHANYA KADAI</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',system-ui,sans-serif;background:#0F172A;display:flex;align-items:center;justify-content:center;height:100vh}
.login-box{background:#fff;border-radius:20px;padding:40px;max-width:400px;width:90%;box-shadow:0 20px 40px rgba(0,0,0,.3);text-align:center}
.login-box .emoji{font-size:48px;margin-bottom:12px}
.login-box h1{font-size:20px;font-weight:800;color:#0F172A;margin-bottom:4px}
.login-box .sub{font-size:13px;color:#64748B;margin-bottom:24px}
.login-box input{width:100%;padding:12px 16px;border:2px solid #E2E8F0;border-radius:12px;font-size:14px;outline:none;margin-bottom:16px;transition:border .2s}
.login-box input:focus{border-color:#2563EB;box-shadow:0 0 0 3px rgba(37,99,235,.15)}
.login-box button{width:100%;padding:12px;background:#2563EB;color:#fff;border:none;border-radius:12px;font-size:14px;font-weight:700;cursor:pointer;transition:background .2s}
.login-box button:hover{background:#1D4ED8}
.login-box .error{color:#DC2626;font-size:12px;margin-top:8px;display:none}
input[type="password"]::-ms-reveal,input[type="password"]::-ms-clear{display:none}
input[type="password"]::-webkit-credentials-auto-fill-button{display:none !important}
</style>
</head>
<body>
<div class="login-box">
<div class="emoji">🌾</div>
<h1>DHANA DHANYA KADAI</h1>
<div class="sub">Enter password to access billing</div>
<form id="lf" onsubmit="return doLogin(event)">
<div style="position:relative">
<input type="password" id="pw" placeholder="Password" autofocus required style="padding-right:44px">
<span id="eye-icon" onclick="togglePassword()" style="position:absolute;right:14px;top:50%;transform:translateY(-50%);cursor:pointer;font-size:18px;user-select:none">👁️</span>
</div>
<button type="submit">🔐 Login</button>
<div class="error" id="err"></div>
</form>
</div>
<script>
function togglePassword(){
  var input=document.getElementById('pw');
  var icon=document.getElementById('eye-icon');
  if(input.type==='password'){input.type='text';icon.innerText='🙈';}
  else{input.type='password';icon.innerText='👁️';}
}
async function doLogin(e){
  e.preventDefault();
  const pw=document.getElementById('pw').value;
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  if(r.ok){window.location.href='/';}
  else{const d=await r.json();const el=document.getElementById('err');el.textContent=d.error||'Wrong password';el.style.display='block';}
  return false;
}
</script>
</body></html>"""

@app.route('/logout')
def logout_route():
    session.clear()
    return redirect('/login')

@app.route('/update-password', methods=['POST'])
@require_login
def update_password_route():
    data = request.get_json()
    old_pw = data.get('old_password', '')
    new_pw = data.get('new_password', '')
    if not new_pw or len(new_pw) < 1:
        return jsonify({'error': 'Password cannot be empty'}), 400
    if not _check_password(old_pw):
        return jsonify({'error': 'Current password is wrong'}), 403
    conn = get_db()
    conn.execute("UPDATE app_settings SET value=? WHERE key='app_password_hash'", (_hash_pw(new_pw),))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'message': 'Password updated'})


# ═══════════════════════════════════════════════════════
# ISSUE 5 – Reset Bill Number
# ═══════════════════════════════════════════════════════
@app.route('/admin/reset-bill-number', methods=['POST'])
@require_login
def reset_bill_number():
    """
    Set the minimum start value for the next bill number.

    POST body: { "start_from": 1 }

    The next bill will be max(current_max_in_db, start_from - 1) + 1,
    so it will never repeat an existing ID and will always be ≥ start_from.
    Existing bills are NEVER modified.
    """
    data = request.get_json() or {}
    try:
        start_from = int(data.get('start_from', 1))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'start_from must be an integer'}), 400

    if start_from < 1:
        return jsonify({'success': False, 'message': 'start_from must be ≥ 1'}), 400

    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)',
        (BILL_NUMBER_START_KEY, str(start_from))
    )
    conn.execute(
        'INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)',
        (BILL_NUMBER_NEXT_KEY, str(start_from))
    )

    # Compute what the actual next bill number will be without consuming it
    actual_next = peek_next_bill_no(conn)
    conn.commit()
    conn.close()

    return jsonify({
        'success': True,
        'message': f'Bill counter reset. Next bill will be #{actual_next}',
        'start_from': start_from,
        'actual_next': actual_next,
    })

# ── Protect all routes except login/logout/static/health ─────────────────────
@app.before_request
def check_login():
    # Always allow these endpoints without auth
    allowed_endpoints = {'login_route', 'logout_route', 'static', 'health', 'serve_index'}
    if request.endpoint in allowed_endpoints:
        return

    # Allow static asset paths (CSS, JS, images, webp)
    static_extensions = ('.css', '.js', '.webp', '.jpg', '.jpeg', '.png', '.ico', '.svg', '.woff', '.woff2')
    if (request.path.startswith('/images/')
            or request.path.startswith('/product_images/')
            or any(request.path.endswith(ext) for ext in static_extensions)):
        return

    # Only /health and /login are public. (Debug raster/hardware-test routes
    # used to be public: /debug/tamil-raster?bill_id=N exposed customer names
    # and phones, and hardware tests could be triggered by any local page.)
    if request.path in ('/health', '/login'):
        return

    if not session.get('logged_in'):
        # API calls get 401 JSON, browser page requests get redirect to login
        if request.is_json or request.headers.get('Accept', '').startswith('application/json'):
            return jsonify({'error': 'Unauthorized'}), 401
        return redirect('/login')


DB_PATH = data_path("billing.db")
IMAGE_FOLDER = data_path("product_images")


# ═══════════════════════════════════════════════════════
# AUTO BACKUP
# ═══════════════════════════════════════════════════════
BACKUP_DIR  = data_path("db_backups")
BACKUP_KEEP_HOURLY = 48   # newest 48 automatic backups (≈2 days at 1/hour)
BACKUP_KEEP_DAILY  = 30   # plus the newest backup of each of the last 30 days
BACKUP_KEEP = BACKUP_KEEP_HOURLY   # backward-compatible name
_BACKUP_NAME_RE = re.compile(r'^billing_(?:(?P<tag>[a-z_]+)_)?(?P<ts>\d{8}_\d{6})\.db$')
_backup_lock = threading.Lock()


def _sqlite_snapshot(dest_path):
    """Consistent copy of the live DB via SQLite's online-backup API, then verify it."""
    tmp_path = dest_path + '.tmp'
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    src = sqlite3.connect(DB_PATH, timeout=30)
    dest = sqlite3.connect(tmp_path)
    try:
        src.backup(dest)
        ok = dest.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        dest.close()
        src.close()
    if ok != 'ok':
        os.remove(tmp_path)
        raise RuntimeError(f'backup integrity_check failed: {ok}')
    os.replace(tmp_path, dest_path)
    return dest_path


def _prune_backups():
    """Retention: newest N automatic backups + newest backup per day for D days.
    Only files named by this app are ever deleted (sorted by embedded timestamp)."""
    autos = []
    for f in os.listdir(BACKUP_DIR):
        m = _BACKUP_NAME_RE.match(f)
        if m and not m.group('tag'):
            autos.append((m.group('ts'), f))
    autos.sort(reverse=True)
    keep = {f for _, f in autos[:BACKUP_KEEP_HOURLY]}
    days = {}
    for ts, f in autos:
        days.setdefault(ts[:8], f)          # first seen = newest of that day
    keep.update(sorted(days.values(), reverse=True)[:BACKUP_KEEP_DAILY])
    for _, f in autos:
        if f not in keep:
            try:
                os.remove(os.path.join(BACKUP_DIR, f))
            except OSError as e:
                app.logger.warning(f'[Backup] Could not prune {f}: {e}')


def _run_backup(tag=None):
    """Snapshot billing.db into db_backups/ (verified), then apply retention.
    Returns True on success, False on failure (failure is logged, never silent)."""
    with _backup_lock:
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name = f"billing_{tag}_{stamp}.db" if tag else f"billing_{stamp}.db"
            dest_path = os.path.join(BACKUP_DIR, name)
            _sqlite_snapshot(dest_path)
            _prune_backups()
            app.logger.info(f"[Backup] Saved + verified → {dest_path}")
            return True
        except Exception as exc:
            app.logger.error(f"[Backup] FAILED: {exc}")
            return False


def _backup_scheduler(interval_seconds: int = 3600):
    """Daemon thread: fire once at startup then every `interval_seconds`."""
    _run_backup()                          # immediate first backup on launch
    while True:
        threading.Event().wait(interval_seconds)
        _run_backup()


def start_backup_scheduler(interval_hours: int = 1):
    """Start the background backup thread. Call once from init_db() or __main__."""
    t = threading.Thread(
        target=_backup_scheduler,
        args=(interval_hours * 3600,),
        daemon=True,
        name="BackupScheduler",
    )
    t.start()
    app.logger.info(f"[Backup] Scheduler started — every {interval_hours}h, keeping {BACKUP_KEEP} copies.")


# ═══════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    # Ensure app_settings exists first to read our optimization flag
    _init_app_settings()
    
    # Check if we've already done the expensive classification pass
    needs_classification = True
    try:
        res = conn.execute("SELECT value FROM app_settings WHERE key='products_classified_v8'").fetchone()
        if res and res['value'] == '1':
            needs_classification = False
    except Exception:
        pass

    if needs_classification:
        try:
            conn.execute("UPDATE products SET nutrient_type = NULL")
            conn.commit()
        except Exception:
            pass  # table may not exist yet

    # New schema with name_english, name_tamil, name_tanglish, price, category, image
    c.execute('''CREATE TABLE IF NOT EXISTS products (
        id              TEXT PRIMARY KEY,
        name_english    TEXT NOT NULL DEFAULT '',
        name_tamil      TEXT NOT NULL DEFAULT '',
        name_tanglish   TEXT NOT NULL DEFAULT '',
        name            TEXT NOT NULL,
        price           REAL NOT NULL DEFAULT 100,
        price_per_kg    REAL NOT NULL DEFAULT 100,
        emoji           TEXT DEFAULT '🌾',
        category        TEXT DEFAULT 'Seeds',
        image           TEXT DEFAULT NULL,
        image_path      TEXT DEFAULT NULL,
        image_filename  TEXT DEFAULT NULL,
        search_tags     TEXT DEFAULT '',
        search_terms    TEXT DEFAULT '[]',
        nutrients       TEXT DEFAULT '{}',
        nutrient_type   TEXT DEFAULT 'normal',
        stock           REAL DEFAULT NULL,
        serial_no       INTEGER DEFAULT NULL
    )''')

    # Migration: add new columns if upgrading from old schema
    for col_def in [
        ('name_english',  'TEXT NOT NULL DEFAULT ""'),
        ('name_tamil',    'TEXT NOT NULL DEFAULT ""'),
        ('name_tanglish', 'TEXT NOT NULL DEFAULT ""'),
        ('price',         'REAL NOT NULL DEFAULT 100'),
        ('image',         'TEXT DEFAULT NULL'),
        ('image_filename','TEXT DEFAULT NULL'),
        ('stock',          'REAL DEFAULT NULL'),
        ('nutrient_type',  "TEXT DEFAULT 'normal'"),
        ('search_terms',   "TEXT DEFAULT '[]'"),
        ('p_rate',         'REAL DEFAULT NULL'),
        ('s_rate',         'REAL DEFAULT NULL'),
        ('wholesale_rate', 'REAL DEFAULT NULL'),
        ('serial_no',      'INTEGER DEFAULT NULL'),
    ]:
        try:
            c.execute(f'ALTER TABLE products ADD COLUMN {col_def[0]} {col_def[1]}')
        except Exception:
            pass

    # Index on serial_no for fast ORDER BY (non-unique: NULL allowed; we enforce uniqueness in app code)
    try:
        c.execute('CREATE INDEX IF NOT EXISTS idx_products_serial_no ON products(serial_no)')
    except Exception:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS bills (
        id              TEXT PRIMARY KEY,
        customer_name   TEXT DEFAULT '',
        customer_phone  TEXT DEFAULT '',
        items           TEXT NOT NULL,
        total           REAL NOT NULL,
        cash            REAL DEFAULT 0,
        balance         REAL DEFAULT 0,
        bill_language   TEXT DEFAULT 'en',
        created_at      TEXT NOT NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS customers (
        phone        TEXT PRIMARY KEY,
        name         TEXT NOT NULL DEFAULT '',
        last_updated TEXT NOT NULL DEFAULT '',
        stars        INTEGER DEFAULT 0
    )''')

    # Add stars column if upgrading from old schema that lacked it
    try:
        c.execute('ALTER TABLE customers ADD COLUMN stars INTEGER DEFAULT 0')
    except Exception:
        pass

    # Migration: drop loyalty_stars column if present (SQLite can't DROP COLUMN
    # before 3.35 — we rebuild the table to stay compatible with older SQLite).
    try:
        _cols = [col[1] for col in c.execute('PRAGMA table_info(customers)').fetchall()]
        if 'loyalty_stars' in _cols:
            c.execute('''CREATE TABLE IF NOT EXISTS customers_clean (
                phone        TEXT PRIMARY KEY,
                name         TEXT NOT NULL DEFAULT '',
                last_updated TEXT NOT NULL DEFAULT '',
                stars        INTEGER DEFAULT 0
            )''')
            c.execute(
                'INSERT OR IGNORE INTO customers_clean (phone, name, last_updated, stars) '
                'SELECT phone, name, last_updated, COALESCE(stars, loyalty_stars, 0) FROM customers'
            )
            c.execute('DROP TABLE customers')
            c.execute('ALTER TABLE customers_clean RENAME TO customers')
            conn.commit()
    except Exception:
        pass

    # Migration: fix old schema where name/last_updated had NOT NULL without DEFAULT.
    # This caused silent crashes when phone was provided but no customer name.
    try:
        _cols_info = c.execute('PRAGMA table_info(customers)').fetchall()
        _nc = next((col for col in _cols_info if col[1] == 'name'), None)
        if _nc and _nc[3] == 1 and _nc[4] is None:
            c.execute('''CREATE TABLE IF NOT EXISTS customers_migrated (
                phone         TEXT PRIMARY KEY,
                name          TEXT NOT NULL DEFAULT \'\',
                last_updated  TEXT NOT NULL DEFAULT \'\',
                stars         INTEGER DEFAULT 0
            )''')
            c.execute('INSERT OR IGNORE INTO customers_migrated SELECT phone, name, last_updated, stars FROM customers')
            c.execute('DROP TABLE customers')
            c.execute('ALTER TABLE customers_migrated RENAME TO customers')
            conn.commit()
    except Exception:
        pass

    # Loyalty settings table
    c.execute('''CREATE TABLE IF NOT EXISTS loyalty_settings (
        id                    INTEGER PRIMARY KEY DEFAULT 1,
        loyalty_enabled       INTEGER DEFAULT 1,
        min_purchase_for_star REAL    DEFAULT 500,
        stars_for_reward      INTEGER DEFAULT 10,
        reward_discount       REAL    DEFAULT 100
    )''')
    # Seed default settings if empty
    c.execute('INSERT OR IGNORE INTO loyalty_settings (id) VALUES (1)')

    # ── Packages table — persistent, admin-managed ──────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS packages (
        id                 TEXT PRIMARY KEY,
        name_english       TEXT NOT NULL DEFAULT '',
        name_tamil         TEXT NOT NULL DEFAULT '',
        emoji              TEXT DEFAULT '📦',
        price_per_kg       REAL NOT NULL DEFAULT 100,
        items              TEXT NOT NULL DEFAULT '[]',
        nutrition_override TEXT DEFAULT NULL,
        created_at         TEXT NOT NULL DEFAULT ''
    )''')
    # Migration: add nutrition_override column if missing (for existing DBs)
    try:
        c.execute("ALTER TABLE packages ADD COLUMN nutrition_override TEXT DEFAULT NULL")
    except Exception:
        pass  # Column already exists
    # Migration: add image column for package cover image
    try:
        c.execute("ALTER TABLE packages ADD COLUMN image TEXT DEFAULT NULL")
    except Exception:
        pass  # Column already exists

    # Seed default packages from hardcoded PACKAGES list if table is empty
    existing_pkg_count = c.execute('SELECT COUNT(*) FROM packages').fetchone()[0]
    if existing_pkg_count == 0:
        for pkg in PACKAGES:
            c.execute(
                'INSERT OR IGNORE INTO packages (id, name_english, name_tamil, emoji, price_per_kg, items, created_at) VALUES (?,?,?,?,?,?,?)',
                (pkg['id'], pkg['name_english'], pkg['name_tamil'], pkg.get('emoji','\U0001f4e6'),
                 pkg['price_per_kg'], json.dumps(pkg['items']), datetime.now().strftime('%d/%m/%Y %H:%M:%S'))
            )

    conn.commit()

    # Seed products — INSERT OR IGNORE ensures all 225 products are present
    # Runs every startup so missing products are added without overwriting existing data
    c.execute('SELECT COUNT(*) FROM products')
    if c.fetchone()[0] == 0:
        # Seed ONLY a brand-new database. The previous "< 100" rule re-inserted
        # deliberately deleted products on every restart of a small catalogue.
        _seed_products(c)
    else:
        # Even when the seed inserts are skipped, we still need the catalog
        # order list so _assign_serial_numbers() can apply S.NO updates.
        _populate_seed_order_only()
    _migrate_legacy_columns(c)
    _migrate_nutrient_structure(c)
    _migrate_bill_item_names(c)
    # Assign / refresh S.NO (catalog order) for every product
    _assign_serial_numbers(c)
    conn.commit()
    # Run strict 3-rule classification if required
    if needs_classification:
        auto_classify_products(conn)
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('products_classified_v8', '1')")
        conn.commit()

    # ── Credit & Due Tracking (v8 addition) ─────────────────────────────────
    # Add total_due column to customers table if not present
    try:
        c.execute('ALTER TABLE customers ADD COLUMN total_due REAL DEFAULT 0')
    except Exception:
        pass  # Already exists

    # Customer created_at (admin manual add + unified customer record)
    try:
        c.execute('ALTER TABLE customers ADD COLUMN created_at TEXT DEFAULT ""')
    except Exception:
        pass

    # Add paid_amount, due_amount, payment_status to bills table
    for col_def in [
        ('paid_amount',     'REAL DEFAULT 0'),
        ('due_amount',      'REAL DEFAULT 0'),
        ('payment_status',  "TEXT DEFAULT 'PAID'"),
        ('payment_method',  "TEXT DEFAULT 'cash'"),
        ('loyalty_discount', 'REAL DEFAULT 0'),     # audit: discount was not recorded
        ('client_bill_ref',  'TEXT DEFAULT NULL'),  # audit: idempotent bill save
    ]:
        try:
            c.execute(f'ALTER TABLE bills ADD COLUMN {col_def[0]} {col_def[1]}')
        except sqlite3.OperationalError as e:
            if 'duplicate column' not in str(e).lower():
                raise
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_bills_client_ref '
              'ON bills(client_bill_ref) WHERE client_bill_ref IS NOT NULL')
    c.execute('CREATE INDEX IF NOT EXISTS idx_bills_customer_phone ON bills(customer_phone)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_payment_history_customer ON payment_history(customer_id)')

    # Payment history table
    c.execute('''CREATE TABLE IF NOT EXISTS payment_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id TEXT NOT NULL,
        amount      REAL NOT NULL,
        date        TEXT NOT NULL,
        method      TEXT DEFAULT 'cash',
        note        TEXT DEFAULT '',
        bill_id     TEXT DEFAULT NULL
    )''')

    conn.commit()
    conn.close()


def _migrate_nutrient_structure(c):
    """
    Runs on every startup. Normalizes nutrients for ALL products unconditionally:
      1. Classifies each product as 'normal' or 'oil' based on fat content.
      2. Converts nutrients to the standard 7-key structure, removing all legacy keys.
    Product IDs, names, and prices are NEVER touched.
    """
    rows = c.execute('SELECT id, nutrients, nutrient_type FROM products').fetchall()
    for row in rows:
        pid     = row[0]
        raw_str = row[1] or '{}'

        try:
            raw_nuts = json.loads(raw_str) if isinstance(raw_str, str) else (raw_str or {})
        except Exception:
            raw_nuts = {}

        # Always re-classify and re-normalize — guarantees clean data after any
        # manual DB edit, new seed, or previous partial migration.
        ntype      = classify_nutrient_type(raw_nuts)
        normalized = normalize_nutrients(raw_nuts, ntype)

        # Belt-and-braces: strip any legacy key that survived normalization
        normalized = {k: v for k, v in normalized.items() if k in ALLOWED_KEYS}

        c.execute(
            'UPDATE products SET nutrients=?, nutrient_type=? WHERE id=?',
            (json.dumps(normalized), ntype, pid)
        )

    # Force-clean DB: use SQLite json_remove to strip legacy keys from any rows
    # that may have been written directly to the DB outside of Python code.
    for legacy_key in ('vitamins', 'minerals', 'carbs'):
        c.execute(
            f"UPDATE products SET nutrients = json_remove(nutrients, '$.{legacy_key}') "
            f"WHERE json_extract(nutrients, '$.{legacy_key}') IS NOT NULL"
        )


def _migrate_legacy_columns(c):
    """Migrate old name_en/name_ta columns to name_english/name_tamil/name_tanglish."""
    try:
        rows = c.execute("SELECT id, name_en, name_ta, name_english, name_tamil, name_tanglish FROM products").fetchall()
    except Exception:
        return
    for row in rows:
        pid = row[0]
        old_en = (row[1] or '').strip()
        old_ta = (row[2] or '').strip()
        new_en = (row[3] or '').strip()
        new_ta = (row[4] or '').strip()
        new_tg = (row[5] or '').strip()

        en = new_en or old_en
        ta = new_ta or old_ta
        tg = new_tg or generate_tanglish_from_tamil(ta)
        display = f"{en} ({ta})" if en and ta else en or ta

        if not new_en or not new_tg:
            c.execute(
                "UPDATE products SET name_english=?, name_tamil=?, name_tanglish=?, name=?, price=price_per_kg WHERE id=?",
                (en, ta, tg, display, pid)
            )


def _migrate_bill_item_names(c):
    """Make persisted bill items bilingual without depending on print language.

    Earlier bills stored `name_tamil`, and still older rows may only have the
    product id. Persist a canonical English `name` plus `tamil_name` for all
    recoverable rows so reprints remain language-selectable.
    """
    try:
        product_rows = c.execute(
            'SELECT id, name_english, name_tamil, name FROM products'
        ).fetchall()
        package_rows = c.execute(
            'SELECT id, name_english, name_tamil FROM packages'
        ).fetchall()
        bill_rows = c.execute('SELECT id, items FROM bills').fetchall()
    except Exception:
        return

    product_names = {
        row[0]: {
            'english': (row[1] or row[3] or '').strip(),
            'tamil': (row[2] or '').strip(),
        }
        for row in product_rows
    }
    product_names.update({
        row[0]: {
            'english': (row[1] or '').strip(),
            'tamil': (row[2] or '').strip(),
        }
        for row in package_rows
    })

    for bill_id, raw_json in bill_rows:
        try:
            items = json.loads(raw_json or '[]')
        except (TypeError, ValueError):
            continue
        if not isinstance(items, list):
            continue

        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            known = product_names.get(item.get('product_id'), {})
            english_name = (
                item.get('name_english') or known.get('english') or
                item.get('name') or ''
            ).strip()
            tamil_name = (
                item.get('tamil_name') or item.get('name_tamil') or
                item.get('name_ta') or item.get('tamil') or
                known.get('tamil') or ''
            ).strip()
            canonical = {
                'name': english_name,
                'name_english': english_name,
                'name_tamil': tamil_name,
                'tamil_name': tamil_name,
            }
            for key, value in canonical.items():
                if item.get(key) != value:
                    item[key] = value
                    changed = True

            for active_item in item.get('active_items') or []:
                if not isinstance(active_item, dict):
                    continue
                ingredient_en = (
                    active_item.get('name_english') or active_item.get('name_en') or
                    active_item.get('name') or ''
                ).strip()
                ingredient_ta = (
                    active_item.get('tamil_name') or active_item.get('name_tamil') or
                    active_item.get('name_ta') or active_item.get('tamil') or ''
                ).strip()
                for key, value in (
                    ('name', ingredient_en),
                    ('name_english', ingredient_en),
                    ('name_tamil', ingredient_ta),
                    ('tamil_name', ingredient_ta),
                ):
                    if active_item.get(key) != value:
                        active_item[key] = value
                        changed = True

        if changed:
            c.execute(
                'UPDATE bills SET items=? WHERE id=?',
                (json.dumps(items, ensure_ascii=False), bill_id)
            )


_SEED_PRODUCT_ORDER = []  # populated by _seed_products(); list of product ids in catalog order

def _seed_products(c):
    """Seed initial product list with id, name_english, name_tamil, name_tanglish, price, category, image, nutrients.

    The order of items in `seed` IS the official catalog order (S.NO 1..N).
    `serial_no` is assigned as the 1-based index of each row.
    """
    # Format: (id, name_english, name_tamil, name_tanglish, price, emoji, category, image, nutrients_dict)
    seed = [
        ("p1","Foxtail Millet","தினை","thinai",100,"🌾","Seeds",None,{"protein":11.5,"fat":4.0,"fiber":8.0,"carbs":67.0,"vitamins":0.5,"minerals":3.3}),
        ("p2","Red Foxtail Millet","செந்தினை","senthinai",100,"🌾","Seeds",None,{"protein":12.3,"fat":4.2,"fiber":8.5,"carbs":65.0,"vitamins":0.5,"minerals":3.1}),
        ("p3","Barnyard Millet","பனிவரகு","panivaragu",100,"🌾","Seeds","pani_varagu.jpg",{"protein":7.7,"fat":1.0,"fiber":8.5,"carbs":75.0,"vitamins":0.3,"minerals":1.7}),
        ("p4","Little Millet","சாமை","samai",100,"🌾","Seeds","samai.jpg",{"protein":7.7,"fat":1.0,"fiber":8.0,"carbs":75.7,"vitamins":0.3,"minerals":1.5}),
        ("p5","Pearl Millet (Native)","நாட்டுகம்பு","naattukambu",100,"🌾","Seeds","pearl_mellet.jpg",{"protein":11.0,"fat":5.0,"fiber":8.5,"carbs":67.0,"vitamins":0.6,"minerals":2.3}),
        ("p6","Kodo Millet","குதிரைவாலி","kuthiraivali",100,"🌾","Seeds","varagu.jpg",{"protein":9.7,"fat":2.0,"fiber":9.4,"carbs":74.0,"vitamins":0.4,"minerals":2.0}),
        ("p7","Varagu Millet","வரகு","varagu",100,"🌾","Seeds","varagu.jpg",{"protein":8.3,"fat":1.1,"fiber":9.7,"carbs":74.9,"vitamins":0.3,"minerals":1.9}),
        ("p8","Browntop Millet","கோராலி","korali",100,"🌾","Seeds","korali.jpg",{"protein":9.0,"fat":2.0,"fiber":7.0,"carbs":74.0,"vitamins":0.3,"minerals":2.0}),
        ("p9","Sunflower Seeds","சூரியகாந்தி விதை","suriyakanthi vithai",100,"🌾","Seeds","sunflower_seed.jpg",{"protein":20.8,"fat":51.5,"fiber":8.6,"carbs":20.0,"vitamins":1.5,"minerals":3.2}),
        ("p1000","Foxtail Millet (Grade 2)","தினை 2","thinai 2",100,"🌾","Seeds","pani_varagu.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p75","Black Sesame Seeds","கருப்பு எள்ளு","karuppu el",100,"🌾","Seeds","black_sesame.jpg",{"protein":17.7,"fat":52.0,"fiber":16.9,"carbs":23.5,"vitamins":0.8,"minerals":9.0}),
        ("p15","White Sesame Seeds","வெள்ள எள்ளு","vella ellu",100,"🌾","Seeds","white_sesame.jpg",{"protein":17.0,"fat":50.0,"fiber":14.0,"carbs":25.0,"vitamins":0.7,"minerals":7.8}),
        ("p16","Paddy (Raw Rice)","நெல்","nel",100,"🌾","Seeds","nel.jpg",{"protein":7.9,"fat":2.7,"fiber":13.2,"carbs":76.0,"vitamins":0.3,"minerals":1.6}),
        ("p18","Buckwheat","பக்வீட்","pakviit",100,"🌾","Seeds","buckwheat.jpg",{"protein":13.2,"fat":3.4,"fiber":10.0,"carbs":71.5,"vitamins":0.4,"minerals":2.2}),
        ("p1001","Alfalfa","குதிரைமசால்","kuthiraimachaal",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p22","Oats","ஓட்ஸ்","oots",100,"🌾","Seeds","oats.jpg",{"protein":16.9,"fat":6.9,"fiber":10.6,"carbs":66.3,"vitamins":0.5,"minerals":3.0}),
        ("p32","Amaranth","அமர்நாத்","amarnaath",100,"🌾","Seeds",None,{"protein":13.6,"fat":7.0,"fiber":6.7,"carbs":65.2,"vitamins":0.5,"minerals":2.7}),
        ("p33","Quinoa","கினோவா","kinoovaa",100,"🌾","Seeds","quinoa.jpg",{"protein":14.1,"fat":6.1,"fiber":7.0,"carbs":64.2,"vitamins":0.5,"minerals":2.5}),
        ("p38","Maize (Corn)","மக்கா சோலம்","makka cholam",100,"🌾","Seeds",None,{"protein":9.4,"fat":4.7,"fiber":2.7,"carbs":74.3,"vitamins":0.4,"minerals":1.2}),
        ("p41","Popcorn","பாப்கார்ன்","paapkaarn",100,"🌾","Seeds",None,{"protein":12.9,"fat":28.1,"fiber":14.5,"carbs":77.9,"vitamins":0.3,"minerals":1.0}),
        ("p45","Wheat","கோதுமை","gothumai",100,"🌾","Seeds","wheat.jpg",{"protein":13.2,"fat":2.5,"fiber":10.7,"carbs":71.2,"vitamins":0.4,"minerals":1.7}),
        ("p51","Finger Millet (Ragi)","கேழ்வாகு","kezhvaragu",100,"🌾","Seeds","ragi.jpg",{"protein":7.3,"fat":1.3,"fiber":11.5,"carbs":72.0,"vitamins":0.4,"minerals":2.7}),
        ("p52","Pearl Millet","கம்பு","kambu",100,"🌾","Seeds","pearl_mellet.jpg",{"protein":11.0,"fat":5.0,"fiber":8.5,"carbs":67.0,"vitamins":0.6,"minerals":2.3}),
        ("p53","Red Rice","சிகப்புஅரிசி","sigappu arisi",100,"🌾","Seeds","red_rice.jpg",{"protein":7.5,"fat":2.0,"fiber":3.5,"carbs":78.0,"vitamins":0.3,"minerals":1.0}),
        ("p54","Black Rice","கருப்புஅரிசி","karuppu arisi",100,"🌾","Seeds","black rice.jpg",{"protein":8.9,"fat":2.0,"fiber":4.5,"carbs":76.2,"vitamins":0.4,"minerals":1.6}),
        ("p59","Barley","பார்லி","paarli",100,"🌾","Seeds",None,{"protein":12.5,"fat":2.3,"fiber":17.3,"carbs":73.5,"vitamins":0.4,"minerals":2.3}),
        ("p71","Chia Seeds","சியாசிட்","chiya seed",100,"🌾","Seeds","chia_seed.jpg",{"protein":16.5,"fat":30.7,"fiber":34.4,"carbs":42.1,"vitamins":0.5,"minerals":5.6}),
        ("p72","Mustard Seeds","கடுகு","kadugu",100,"🌾","Seeds","mustard.jpg",{"protein":26.1,"fat":36.0,"fiber":12.2,"carbs":28.1,"vitamins":0.6,"minerals":4.3}),
        ("p76","Fenugreek Seeds","வெந்தயம்","venthayam",100,"🧂","Supplements","fenugreek.jpg",{"protein":23.0,"fat":6.4,"fiber":24.6,"carbs":58.4,"vitamins":0.5,"minerals":3.4}),
        ("p82","Rolled Oats","ரொல்ட்டு ஓட்ஸ்","rolttu oots",100,"🌾","Seeds","rolled oats.jpg",{"protein":13.2,"fat":7.5,"fiber":10.1,"carbs":67.7,"vitamins":0.5,"minerals":2.8}),
        ("p91","Raisins","திராச்சை","thiratchai",100,"🧂","Supplements","raisins.jpg",{"protein":3.1,"fat":0.5,"fiber":3.7,"carbs":79.2,"vitamins":0.3,"minerals":1.8}),
        ("p106","Rice","அரிசி","arisi",100,"🌾","Seeds","rice.jpg",{"protein":7.5,"fat":2.2,"fiber":3.5,"carbs":78.2,"vitamins":0.3,"minerals":1.0}),
        ("p116","Wild Turmeric (Kasturi)","கஸ்தூரி மஞ்சள்","kasturi manjal",100,"🧂","Supplements","turmeric.jpg",{"protein":9.7,"fat":3.3,"fiber":21.1,"carbs":67.1,"vitamins":0.8,"minerals":4.4}),
        ("p141","Cumin Seeds","சீரகம்","seeragam",100,"🧂","Supplements","cumin.jpg",{"protein":17.8,"fat":22.3,"fiber":10.5,"carbs":44.2,"vitamins":0.7,"minerals":7.7}),
        ("p145","Poppy Seeds","கசகசா","kaskas",100,"🌾","Seeds","poppy_seed.jpg",{"protein":18.0,"fat":41.6,"fiber":19.5,"carbs":28.1,"vitamins":0.6,"minerals":9.1}),
        ("p173","Coriander Seeds","தனியா","thaniya",100,"🧂","Supplements",None,{"protein":21.9,"fat":17.8,"fiber":41.9,"carbs":54.9,"vitamins":1.0,"minerals":5.5}),
        ("p176","Honey","தேன்","then",100,"🧂","Supplements","honey.jpg",{"protein":0.3,"fat":0.0,"fiber":0.2,"carbs":82.4,"vitamins":0.1,"minerals":0.2}),
        ("p180","Dates","பேரிச்சம்பழம்","pericham pazham",100,"🧂","Supplements",None,{"protein":2.5,"fat":0.4,"fiber":8.0,"carbs":75.0,"vitamins":0.2,"minerals":1.0}),
        ("p184","Cucumber Seeds","வெள்ளரி விதை","vellari vithai",100,"🌾","Seeds","cucumber_seed.jpg",{"protein":24.0,"fat":44.8,"fiber":2.5,"carbs":11.0,"vitamins":0.4,"minerals":2.1}),
        ("p188","Pumpkin Seeds","பூசனி விதை","pusani vithai",100,"🌾","Seeds","pumpkin seed.jpg",{"protein":30.2,"fat":49.1,"fiber":6.0,"carbs":10.7,"vitamins":0.5,"minerals":5.4}),
        ("p1005","Banyan Tree Seeds","ஆலம் விதை","aalam vithai",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1006","Peepal Tree Seeds","அரச விதை","aracha vithai",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1019","Senna Seeds","தேத்தான் கொட்டை","theeththaan kottaை",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p227","Moringa Seeds","முருங்கை விதை","murungai vithai",300,"🌱","Seeds",None,{"protein":35.0,"fat":38.0,"fiber":2.5,"carbs":8.0,"vitamins":1.2,"minerals":5.0}),
        ("p1030","Onion Seeds","வெங்காய விதை","vengkaaya vithai",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1031","Radish Seeds","முள்ளங்கி விதை","mullangki vithai",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1047","Foxtail Millet (Fine Variety)","இனை","inai",100,"🌾","Seeds","pani_varagu.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1048","Korai Grass Seeds","கோரைவி","kooraivi",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1049","Sunflower Stalk Seeds","சூரியகாந்தி கோடு","chuuriyakaanthi kootu",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1050","Large Sunflower Seeds","பெரியகுரயகாந்தி","periyakurayakaanthi",100,"🌾","Seeds","sunflower_seed.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1051","Black Sunflower Seeds","சூரியகாந்தி (கருப்பு)","chuuriyakaanthi (karuppu)",100,"🌾","Seeds","sunflower_seed.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1052","Safflower Seeds","சப்போலா","chappoolaa",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1053","Flax Seeds","ஆலில்","aalil",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1054","Niger Seeds (Ramtil)","ராம்தேல்","raamtheel",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1055","Rapeseed (Canola)","ரேப்சீடு","reepchiitu",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1056","Sesame Seeds (Gouri Variety)","கௌரி சீடு","kaௌri chiitu",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1057","Field Oats","பீல்டு ஓட்ஸ","piiltu ootsa",100,"🌾","Seeds","oats.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1058","Long Cut Oats","லாங் ஓட்ஸ","laang ootsa",100,"🌾","Seeds","oats.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1059","Milk Thistle Seeds","பால் நெருஞ்சில்","paal nerunjchil",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1060","Red Millet","ரெட்மில்லட்","retmillat",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1061","Black Millet","பிலக்மில்லட்","pilakmillat",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1062","Wheat Millet","வீட்மில்லட்","viitmillat",100,"🌾","Seeds","wheat.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1063","Green Millet","கிரீன்மில்லட்","kiriinmillat",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1064","Yellow Millet","எல்லோமில்லட்","elloomillat",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1066","Dark Sorghum","இருங்குசோலாம்","irungkuchoolaam",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1069","Sweet Corn","எஸ் மக்கா","es makkaa",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1070","Dried Field Corn","உட்சர்சமக்கா","utcharchamakkaa",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p320","Samba Wheat","சம்பாகோதுமை","samba gothumai",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1072","White Sorghum","வெள்ளாசோலம்","vellaachoolam",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1073","Red Sorghum","சிகப்புசோலம்","chikappuchoolam",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1344b","Pearl Millet (Kambu)","கேம்வாகு","keemvaagu",100,"🌾","Seeds","pearl_mellet.jpg",{"protein":11.0,"fat":5.0,"fiber":8.5,"carbs":67.0,"vitamins":0.6,"minerals":2.3}),
        ("p325","Bamboo Rice","முங்கில் அரிசி","moongil arisi",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p327","Mappillai Samba Rice","மாப்பிள்ளைசம்பா","mappillai samba",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1075","Barley Husk","பார்வி அஸ்க்","paarvi ask",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1077","Namakkaramani Rice","நாமகாரமணி","naamakaaramani",100,"🌾","Seeds","rice.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p333","Fox Millet","நரிபயிர்","nari payir",100,"🌾","Seeds","sen_thinai.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1079","Emmer Wheat","இரிட்","irit",100,"🌾","Seeds","wheat.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1080","Hulled Sunflower Seeds","பில்டு சூரியகாந்தி","piltu chuuriyakaanthi",100,"🌾","Seeds","sunflower_seed.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1357b","Fenugreek Seeds","வேந்தயம்","venthayam",100,"🧂","Supplements","fenugreek.jpg",{"protein":23.0,"fat":6.4,"fiber":24.6,"carbs":58.4,"vitamins":0.5,"minerals":3.4}),
        ("p1082","Striped Sunflower Seeds","வள்ள-சூரியகாந்தி","valla-chuuriyakaanthi",100,"🌾","Seeds","sunflower_seed.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p339","Black Wheat","கருப்பு கோதுமை","karuppu gothumai",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1086","Alfalfa Greens","குதிரைமசால் கிரை","kuthiraimachaal kirai",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1087","Sunflower Seeds (Vinola Variety)","சூரியகாந்தி-வினோலா","chuuriyakaanthi-vinaோlaa",100,"🌾","Seeds","sunflower_seed.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1366b","Moringa Seeds","முருங்கைவிதை","murungaivithai",300,"🌱","Seeds",None,{"protein":35.0,"fat":38.0,"fiber":2.5,"carbs":8.0,"vitamins":1.2,"minerals":5.0}),
        ("p347","Karunguruvai Black Rice","கருங்குறுவை","karunguruvai",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p349","Foxtail Millet Rice","இனை அரிசி","inai arisi",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p350","Varagu Millet Rice","வரகு அரிசி","varagu arisi",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p351","Little Millet Rice","சாமை அரிசி","samai arisi",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p352","Kodo Millet Rice","குதிரைவாலி அரிசி","kuthiraivali arisi",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1094","White Sesame (Gingelly)","சிரியாவி (சானியல்)","chiriyaavi (chaaniyal)",100,"🌾","Seeds","white_sesame.jpg",{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1096","Wild Tamarind Seeds (Kadamozhu)","கடமொழு","katamozhu",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p365","Seven Seeds Mix","சப்துவிதை","saptha vithai",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1105","Peepal Tree Seeds","அரசு விதை","arachu vithai",100,"🌾","Seeds",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1412b","Cucumber Seeds","வேள்ளரி விதை","veellari vithai",100,"🌾","Seeds","cucumber_seed.jpg",{"protein":24.0,"fat":44.8,"fiber":2.5,"carbs":11.0,"vitamins":0.4,"minerals":2.1}),
        ("p1203","Pealed Sunflower Seed","உரித்த சூரியகாந்தி விதை","uriththaa suriyakanthi vithai",100,"🌾","Seeds",None,{"protein":20.8,"fat":51.5,"fiber":8.6,"carbs":20.0,"vitamins":1.5,"minerals":3.2}),
        ("p1204","Tham Powder","தம் பொடி","tham podi",100,"🧂","Supplements",None,{"protein":8.0,"fat":2.0,"fiber":6.0,"carbs":70.0,"vitamins":0.3,"minerals":1.5}),
        ("p1422b","Pumpkin Seeds","பூசணி விதை","pusani vithai 2",100,"🌾","Seeds","pumpkin seed.jpg",{"protein":30.2,"fat":49.1,"fiber":6.0,"carbs":10.7,"vitamins":0.5,"minerals":5.4}),
    ]

    # Expose catalog order at module level so other migrations can use it
    global _SEED_PRODUCT_ORDER
    _SEED_PRODUCT_ORDER = [r[0] for r in seed]

    for idx, row in enumerate(seed, start=1):
        pid, name_en, name_ta, name_tg, price, emoji, cat, img, nuts = row
        if not name_tg:
            name_tg = generate_tanglish_from_tamil(name_ta)
        display = f"{name_en} ({name_ta})" if name_en and name_ta else name_en or name_ta

        # Build search terms: english words + tamil + tanglish
        search_terms = []
        search_terms.extend(name_en.lower().split())
        search_terms.append(name_ta)
        search_terms.extend(name_tg.lower().split())
        # Add common alternates
        search_terms.append(name_en.lower())
        search_terms.append(name_tg.lower())
        search_terms = list(dict.fromkeys(search_terms))  # deduplicate

        # Resolve image filename: use provided seed img only if it ends with .webp/.png/.gif
        # (legacy .jpg refs from old seed data are not present in frontend/images/)
        clean_img = img if (img and not img.lower().endswith('.jpg') and not img.lower().endswith('.jpeg')) else None

        # Normalize nutrients at seed time
        ntype = classify_nutrient_type(nuts)
        normalized_nuts = normalize_nutrients(nuts, ntype)

        c.execute('''INSERT OR IGNORE INTO products
                     (id, name_english, name_tamil, name_tanglish, name,
                      price, price_per_kg, emoji, category, image, image_path, image_filename,
                      search_tags, search_terms, nutrients, nutrient_type, serial_no)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (pid, name_en, name_ta, name_tg, display,
                   price, price, emoji, cat, clean_img, clean_img, clean_img,
                   name_tg,
                   json.dumps(search_terms),
                   json.dumps(normalized_nuts),
                   ntype, idx))


def _populate_seed_order_only():
    """Populate _SEED_PRODUCT_ORDER without inserting/changing any rows.

    Calls _seed_products() against an in-memory throwaway SQLite DB so the
    seed-list source-of-truth stays in one place.
    """
    global _SEED_PRODUCT_ORDER
    if _SEED_PRODUCT_ORDER:
        return
    try:
        import sqlite3 as _sqlite
        _tmp = _sqlite.connect(':memory:')
        _tmp.execute('''CREATE TABLE products (
            id TEXT PRIMARY KEY, name_english TEXT, name_tamil TEXT, name_tanglish TEXT,
            name TEXT, price REAL, price_per_kg REAL, emoji TEXT, category TEXT,
            image TEXT, image_path TEXT, image_filename TEXT, search_tags TEXT,
            search_terms TEXT, nutrients TEXT, nutrient_type TEXT, serial_no INTEGER
        )''')
        _seed_products(_tmp.cursor())
        _tmp.close()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# SERIAL NUMBER (S.NO) — catalog ordering
# ──────────────────────────────────────────────────────────────────────────────
# Catalog order is defined by the position of products in `_seed_products()` seed list.
# `serial_no` is the canonical ordering key used everywhere in the UI.
#  - Seeded products: serial_no = (index in seed list, 1-based)
#  - Existing pre-existing products without serial_no: assigned sequential numbers
#    after the highest seeded serial_no, ordered by name_english (alphabetical).
#  - New admin-added products: auto-assigned MAX(serial_no)+1 unless caller provides one.
# This function is idempotent: it only fills rows where serial_no IS NULL.
def _assign_serial_numbers(c):
    """Backfill serial_no for any product that doesn't have one yet.

    Strategy:
      1) For each (id -> seed_index) mapping in the current seed list, force the
         serial_no to match the seed index. This guarantees catalog products keep
         their canonical S.NO even if the admin previously edited them.
      2) For every remaining product with serial_no IS NULL, assign the next
         available number (MAX+1, MAX+2, ...) ordered alphabetically by
         name_english as a stable, predictable fallback.
    """
    # Step 1 — sync seeded products to their catalog index
    try:
        for idx, pid in enumerate(_SEED_PRODUCT_ORDER, start=1):
            c.execute('UPDATE products SET serial_no=? WHERE id=?', (idx, pid))
    except Exception:
        pass

    # Step 2 — assign next-available numbers to remaining NULL rows
    row = c.execute('SELECT COALESCE(MAX(serial_no), 0) FROM products').fetchone()
    next_no = (row[0] or 0) + 1
    rows = c.execute(
        "SELECT id FROM products WHERE serial_no IS NULL "
        "ORDER BY LOWER(COALESCE(name_english, name)) ASC"
    ).fetchall()
    for r in rows:
        c.execute('UPDATE products SET serial_no=? WHERE id=?', (next_no, r['id'] if hasattr(r, 'keys') else r[0]))
        next_no += 1


def _get_loyalty_settings_data(conn=None):
    """Return loyalty settings dict from DB (never hardcoded).
    Accepts an existing connection to avoid opening a second connection
    while a transaction is in progress (prevents SQLite lock errors).
    """
    close_after = False
    if conn is None:
        conn = get_db()
        close_after = True
    try:
        row = conn.execute('SELECT * FROM loyalty_settings WHERE id=1').fetchone()
    finally:
        if close_after:
            conn.close()
    if row:
        return {
            'enabled':        bool(row['loyalty_enabled']),
            'min_purchase':   float(row['min_purchase_for_star']),
            'stars_required': int(row['stars_for_reward']),
            'discount':       float(row['reward_discount']),
        }
    # Fallback defaults (only if table is somehow empty)
    return {'enabled': True, 'min_purchase': 500.0, 'stars_required': 5, 'discount': 50.0}


def row_to_dict(row):
    d = dict(row)

    # ── Parse JSON fields ─────────────────────────────────────────────────────
    for key in ('nutrients', 'items', 'search_terms'):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                d[key] = {} if key == 'nutrients' else []

    # ── Nutrient type: resolve with explicit fallback chain ───────────────────
    # Priority: DB column → classify from raw → default 'normal'
    raw_nuts = d.get('nutrients') or {}
    stored_type = (d.get('nutrient_type') or '').strip()
    if stored_type not in ('normal', 'oil', 'other'):
        stored_type = classify_nutrient_type(raw_nuts)
    d['nutrient_type'] = stored_type

    # ── STRICT: ALWAYS normalize — no raw JSON ever leaves this function ──────
    nutrients = normalize_nutrients(raw_nuts, d['nutrient_type'])
    # Guarantee output is exactly ALLOWED_KEYS — strip any legacy survivor
    nutrients = {k: (nutrients.get(k) or 0.0) for k in STANDARD_NUTRIENT_KEYS}
    nutrients = {k: v for k, v in nutrients.items() if k in ALLOWED_KEYS}
    d['nutrients'] = nutrients

    # ── Nutrient category label for frontend display ──────────────────────────
    d['nutrient_category'] = (
        'Oil Content Grains' if d['nutrient_type'] == 'oil'
        else 'Other Products'  if d['nutrient_type'] == 'other'
        else 'Normal Grains'
    )

    # ── Ensure name fields are present and correct ────────────────────────────
    en = (d.get('name_english') or d.get('name_en') or '').strip()
    ta = (d.get('name_tamil') or d.get('name_ta') or '').strip()
    tg = (d.get('name_tanglish') or '').strip()
    if not tg and ta:
        tg = generate_tanglish_from_tamil(ta)

    d['name_english'] = en
    d['name_tamil'] = ta
    d['name_tanglish'] = tg  # NOT shown in UI, search only
    d['price'] = d.get('price') or d.get('price_per_kg', 100)
    d['price_per_kg'] = d['price']
    # Display name: English (Tamil) only
    d['display_name'] = f"{en} ({ta})" if en and ta else en or ta or d.get('name', '')
    d['name'] = d['display_name']

    # ── Image field: exact filename, discard legacy .jpg refs ─────────────────
    img = (d.get('image_filename') or d.get('image') or d.get('image_path') or '').strip()
    if img.lower().endswith('.jpg') or img.lower().endswith('.jpeg'):
        img = ''
    # Auto-map image if not set
    if not img:
        img = find_product_image(en)
    d['image']          = img
    d['image_filename'] = img
    d['image_path']     = img

    return d


# ═══════════════════════════════════════════════════════
# IMAGE SERVING
# ═══════════════════════════════════════════════════════
FRONTEND_DIR = resource_path("frontend")

# ═══════════════════════════════════════════════════════
# AUTO IMAGE MAPPING — cached file list + smart matching
# ═══════════════════════════════════════════════════════
_IMAGE_CACHE = None  # {normalized_name: filename}

def _build_image_cache():
    """Scan frontend/images/ once and build lookup dict."""
    global _IMAGE_CACHE
    img_dir = os.path.join(FRONTEND_DIR, 'images')
    _IMAGE_CACHE = {}
    if not os.path.isdir(img_dir):
        return
    for fname in os.listdir(img_dir):
        if not fname.lower().endswith('.webp'):
            continue
        # Normalize: strip extension, lowercase, remove special chars, spaces→underscore
        base = os.path.splitext(fname)[0]
        key = _normalize_image_key(base)
        _IMAGE_CACHE[key] = fname

def _normalize_image_key(name):
    """Normalize a name for image matching: lowercase, strip brackets/special, spaces→_"""
    s = name.lower()
    s = re.sub(r'\([^)]*\)', '', s)       # remove (bracketed) text
    s = re.sub(r'[^a-z0-9\s_]', '', s)    # keep alnum + spaces + underscores
    s = re.sub(r'[\s_]+', '_', s.strip())  # spaces/underscores → single underscore
    s = s.strip('_')
    return s


# ── Manual aliases for products whose names don't match any image file ────────
_PRODUCT_IMAGE_ALIASES = {
    'soybean': 'soya.webp',
    'soy': 'soya.webp',
    'turmeric_powder': 'turmeric_podwer.webp',
    'turmeric': 'turmeric_podwer.webp',
    'limestone_powder': 'lime_stone.webp',
    'limestone': 'lime_stone.webp',
    'lime_stone_powder': 'lime_stone.webp',
    'eggshell': 'egg_shell.webp',
    'eggshell_powder': 'egg_shell.webp',
    'almonds': 'almond.webp',
    'raisins': 'raisin.webp',
    'red_cowpea': 'red_cow_pea.webp',
    'salaiyal': 'saliyal.webp',
}

def find_product_image(product_name):
    """
    Auto-match product name to an image file.
    1. Exact match on normalized name
    2. Partial match: if all words of an image key appear in product key (or vice versa)
    3. Fallback: default.webp
    """
    if _IMAGE_CACHE is None:
        _build_image_cache()
    if not product_name:
        return 'default.webp'

    key = _normalize_image_key(product_name)
    # Exact match
    if key in _IMAGE_CACHE:
        return _IMAGE_CACHE[key]

    # Alias match
    if key in _PRODUCT_IMAGE_ALIASES:
        return _PRODUCT_IMAGE_ALIASES[key]

    # Partial match: product words vs image words
    prod_words = set(key.split('_'))
    prod_words.discard('')
    best_match = None
    best_score = 0
    for img_key, img_fname in _IMAGE_CACHE.items():
        img_words = set(img_key.split('_'))
        img_words.discard('')
        # Check if all image words are in product name or vice versa
        common = prod_words & img_words
        if len(common) >= 1:
            # Score: prefer more matching words, penalize extra unmatched words
            score = len(common) * 2 - abs(len(prod_words) - len(img_words))
            if score > best_score:
                best_score = score
                best_match = img_fname

    # Only accept partial match if at least 2 words match, or single-word product matches fully
    if best_match and (best_score >= 2 or len(prod_words) == 1):
        return best_match

    return 'default.webp'


@app.route('/product_images/<filename>')
def serve_product_image(filename):
    return send_from_directory(IMAGE_FOLDER, filename)

@app.route('/images/<filename>')
def serve_frontend_image(filename):
    return send_from_directory(os.path.join(FRONTEND_DIR, 'images'), filename)


# ═══════════════════════════════════════════════════════
# PRODUCTS  (read)
# ═══════════════════════════════════════════════════════
@app.route('/products', methods=['GET'])
@app.route('/api/products', methods=['GET'])
def get_products():
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM products '
        'ORDER BY CASE WHEN serial_no IS NULL THEN 1 ELSE 0 END, '
        '         serial_no ASC, '
        '         LOWER(COALESCE(name_english, name)) ASC'
    ).fetchall()
    conn.close()
    return jsonify({'success': True, 'products': [row_to_dict(r) for r in rows]})


# ═══════════════════════════════════════════════════════
# PACKAGES  (read)
# ═══════════════════════════════════════════════════════
@app.route('/packages', methods=['GET'])
def get_packages():
    """Load all packages from DB (was previously hardcoded)."""
    conn = get_db()
    rows = conn.execute('SELECT * FROM packages ORDER BY created_at').fetchall()
    conn.close()
    clean_packages = []
    for row in rows:
        try:
            items = json.loads(row['items'] or '[]')
        except Exception:
            items = []
        clean_items = []
        for item in items:
            raw   = item.get('nutrients_per_100g', {})
            ntype = classify_nutrient_type(raw)
            norm  = normalize_nutrients(raw, ntype)
            norm  = {k: (norm.get(k) or 0.0) for k in STANDARD_NUTRIENT_KEYS}
            norm  = {k: v for k, v in norm.items() if k in ALLOWED_KEYS}
            clean_items.append({**item, 'nutrients_per_100g': norm})
        _nutr_ov = json.loads(row['nutrition_override']) if row['nutrition_override'] else None
        _pkg_product_mode = (_nutr_ov or {}).get('_product_mode', 'normal')
        clean_packages.append({
            'id':                 row['id'],
            'name_english':       row['name_english'],
            'name_tamil':         row['name_tamil'],
            'emoji':              row['emoji'] or '📦',
            'category':           'Packages',
            'price_per_kg':       row['price_per_kg'],
            'is_package':         True,
            'items':              clean_items,
            'nutrition_override': _nutr_ov,
            'product_mode':       _pkg_product_mode,
            'image':              row['image'] if 'image' in row.keys() else None,
        })
    return jsonify({'success': True, 'packages': clean_packages})


@app.route('/admin/packages', methods=['POST'])
def admin_add_package():
    """Create a new package."""
    """Create a new package."""
    data = request.get_json(silent=True) or {}
    name_en = (data.get('name_english') or '').strip()
    if not name_en:
        return jsonify({'success': False, 'message': 'Package name required'}), 400
    _err = _validate_admin_prices(data)
    if _err:
        return jsonify({'success': False, 'message': _err}), 400
    pkg_id = 'pkg' + str(int(datetime.now().timestamp() * 1000))[-6:]
    items  = data.get('items', [])
    product_mode = data.get('product_mode', 'normal')
    nutr_override = data.get('nutrition_override')
    # Store product_mode inside nutrition_override JSON (no schema change needed)
    if nutr_override:
        nutr_override['_product_mode'] = product_mode
    else:
        nutr_override = {'_product_mode': product_mode}
    conn   = get_db()
    conn.execute(
        'INSERT INTO packages (id, name_english, name_tamil, emoji, price_per_kg, items, nutrition_override, created_at) VALUES (?,?,?,?,?,?,?,?)',
        (pkg_id, name_en, data.get('name_tamil',''), data.get('emoji','📦'),
         float(data.get('price_per_kg', 100)),
         json.dumps(items),
         json.dumps(nutr_override) if nutr_override else None,
         datetime.now().strftime('%d/%m/%Y %H:%M:%S'))
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'id': pkg_id})


@app.route('/admin/packages/<pkg_id>', methods=['PUT'])
def admin_update_package(pkg_id):
    """Update an existing package."""
    data  = request.get_json() or {}
    conn  = get_db()
    row   = conn.execute('SELECT id FROM packages WHERE id=?', (pkg_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': 'Package not found'}), 404
    name_en = (data.get('name_english') or '').strip()
    if not name_en:
        conn.close()
        return jsonify({'success': False, 'message': 'Package name required'}), 400
    _err = _validate_admin_prices(data)
    if _err:
        conn.close()
        return jsonify({'success': False, 'message': _err}), 400
    items = data.get('items', [])
    product_mode = data.get('product_mode', 'normal')
    nutr_override = data.get('nutrition_override')
    if nutr_override:
        nutr_override['_product_mode'] = product_mode
    else:
        nutr_override = {'_product_mode': product_mode}
    conn.execute(
        'UPDATE packages SET name_english=?, name_tamil=?, emoji=?, price_per_kg=?, items=?, nutrition_override=? WHERE id=?',
        (name_en, data.get('name_tamil',''), data.get('emoji','📦'),
         float(data.get('price_per_kg', 100)), json.dumps(items),
         json.dumps(nutr_override) if nutr_override else None,
         pkg_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/admin/packages/<pkg_id>/image', methods=['POST'])
def upload_package_image(pkg_id):
    """Upload a cover image for a package."""
    data = request.get_json() or {}
    img_data = data.get('image_data', '')  # base64 data URL
    if not img_data:
        return jsonify({'success': False, 'message': 'No image data'}), 400
    try:
        import base64, re as _re
        # Strip the data URL prefix
        m = _re.match(r'data:image/([a-zA-Z]+);base64,(.+)', img_data)
        if not m:
            return jsonify({'success': False, 'message': 'Invalid image format'}), 400
        ext = m.group(1).lower()
        if ext == 'jpeg': ext = 'jpg'
        raw = base64.b64decode(m.group(2))
        filename = f'pkg_{pkg_id}.{ext}'
        images_dir = os.path.join(resource_path('frontend'), 'images')
        os.makedirs(images_dir, exist_ok=True)
        filepath = os.path.join(images_dir, filename)
        with open(filepath, 'wb') as f2:
            f2.write(raw)
        # Save filename to DB
        conn = get_db()
        conn.execute('UPDATE packages SET image=? WHERE id=?', (filename, pkg_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'image': filename})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/admin/packages/<pkg_id>', methods=['DELETE'])
def admin_delete_package(pkg_id):
    """Delete a package."""
    conn = get_db()
    conn.execute('DELETE FROM packages WHERE id=?', (pkg_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════
# PACKAGE NUTRITION  — live preview for package edit modal
# ═══════════════════════════════════════════════════════
@app.route('/api/package-nutrition', methods=['POST'])
def api_package_nutrition():
    """
    Calculate weighted-average nutrition for a set of package items.
    Called by the frontend package modal whenever a user edits ingredient weights.

    POST body:
    {
        "items": [
            {
                "name_english": "Groundnut",
                "name_tamil":   "வேற்கடல",
                "weight_kg":    1.5,
                "nutrients_per_100g": {
                    "protein": 25.8, "fat": 49.2, ...
                }
            },
            ...
        ]
    }

    Returns:
    {
        "success": true,
        "nutrition": { "protein": 13.2, "fat": 7.1, ... },   // weighted-avg %
        "has_oil":   true,
        "has_normal": true,
        "total_weight_kg": 53.0
    }
    """
    data  = request.get_json() or {}
    items = data.get('items', [])
    if not items:
        return jsonify({'success': False, 'message': 'No items provided'}), 400

    result = calculate_package_nutrition(items)
    return jsonify({
        'success':          True,
        'nutrition':        result['nutrition'],
        'has_oil':          result['has_oil'],
        'has_normal':       result['has_normal'],
        'total_weight_kg':  result['total_weight_kg'],
    })


# ═══════════════════════════════════════════════════════
# API ALIASES  (for /api/products POST and PUT)
# ═══════════════════════════════════════════════════════
@app.route('/api/products', methods=['POST'])
def api_add_product():
    return add_product()

@app.route('/api/products/<pid>', methods=['PUT'])
def api_update_product(pid):
    return update_product(pid)

@app.route('/api/products/<pid>', methods=['DELETE'])
def api_delete_product(pid):
    return delete_product(pid)


# ═══════════════════════════════════════════════════════
# ADMIN – Add product
# ═══════════════════════════════════════════════════════
def _validate_admin_prices(data):
    """Reject NaN/inf/negative/non-numeric prices before they reach billing."""
    for key in ('price', 'price_per_kg', 's_rate', 'p_rate', 'wholesale_rate'):
        val = data.get(key)
        if val in (None, ''):
            continue
        try:
            _finite_number(val, key, minimum=0, maximum=MAX_MONEY)
        except BillValidationError as ve:
            return str(ve)
    return None


@app.route('/admin/products', methods=['POST'])
def add_product():
    data = request.get_json(silent=True)
    if not data or not str(data.get('name_english') or '').strip():
        return jsonify({'success': False, 'message': 'English name required'}), 400

    _err = _validate_admin_prices(data)
    if _err:
        return jsonify({'success': False, 'message': _err}), 400
    pid      = 'p' + str(uuid.uuid4())[:8]
    name_en  = data['name_english'].strip()
    name_ta  = data.get('name_tamil', '').strip()
    if not name_ta:
        name_ta = translate_to_tamil(name_en)
    name_tg  = data.get('name_tanglish', '').strip()
    if not name_tg and name_ta:
        name_tg = generate_tanglish_from_tamil(name_ta)  # auto-generate if missing

    name_d   = f"{name_en} ({name_ta})" if name_ta else name_en
    p_rate = data.get('p_rate'); p_rate = float(p_rate) if p_rate not in (None,'') else None
    s_rate = data.get('s_rate'); s_rate = float(s_rate) if s_rate not in (None,'') else None
    wrate  = data.get('wholesale_rate'); wrate = float(wrate) if wrate not in (None,'') else None
    # Validate all 3 prices are provided
    if s_rate is None:
        return jsonify(success=False, message='S.Rate (Selling rate) is required'), 400
    if p_rate is None:
        return jsonify(success=False, message='P.Rate (Purchase rate) is required'), 400
    if wrate is None:
        return jsonify(success=False, message='Wholesale rate is required'), 400
    price  = float(data.get('price', data.get('price_per_kg', s_rate or p_rate or 100)))
    emoji  = data.get('emoji', '🌾')
    cat    = data.get('category', 'Seeds')
    # Always include both Tamil script and Tanglish in search_tags
    user_tags = data.get('search_tags', '')
    tags = ' '.join(filter(None, [name_ta, name_tg, user_tags])).strip()
    raw_nuts  = data.get('nutrients', {})
    # ── Safe-parse every nutrient value to float ──
    for k in list(raw_nuts.keys()):
        raw_nuts[k] = get_float(raw_nuts[k])
    ntype     = data.get('nutrient_type') or classify_nutrient_type(raw_nuts)
    if ntype not in ('normal', 'oil'):
        ntype = classify_nutrient_type(raw_nuts)
    normalized_nuts = normalize_nutrients(raw_nuts, ntype)
    nuts      = json.dumps(normalized_nuts)
    img       = data.get('image', '')

    search_terms = json.dumps([name_en.lower(), name_ta, name_tg.lower()])

    conn = get_db()

    # ── Assign serial_no (S.NO) ──────────────────────────────────────────────
    # Honor an explicit serial_no from the admin form when provided; otherwise
    # auto-assign the next available number (MAX+1) so it always sorts last.
    serial_in = data.get('serial_no')
    try:
        serial_no = int(serial_in) if serial_in not in (None, '', 'null') else None
    except (TypeError, ValueError):
        serial_no = None
    if serial_no is None:
        row = conn.execute('SELECT COALESCE(MAX(serial_no), 0) FROM products').fetchone()
        serial_no = (row[0] or 0) + 1

    conn.execute('''INSERT INTO products
                    (id, name_english, name_tamil, name_tanglish, name,
                     price, price_per_kg, emoji, category, image, image_path, image_filename,
                     search_tags, search_terms, nutrients, nutrient_type,
                     p_rate, s_rate, wholesale_rate, serial_no)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                 (pid, name_en, name_ta, name_tg, name_d,
                  price, price, emoji, cat, img, img, img, tags, search_terms, nuts, ntype,
                  p_rate, s_rate, wrate, serial_no))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'id': pid, 'serial_no': serial_no}), 201


# ═══════════════════════════════════════════════════════
# ADMIN – Update product
# ═══════════════════════════════════════════════════════
@app.route('/admin/products/<pid>', methods=['PUT'])
def update_product(pid):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': 'JSON body required'}), 400
    _err = _validate_admin_prices(data)
    if _err:
        return jsonify({'success': False, 'message': _err}), 400
    conn = get_db()
    row  = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': 'Not found'}), 404

    name_en  = data.get('name_english', row['name_english'] or '').strip()
    name_ta  = data.get('name_tamil', row['name_tamil'] or '').strip()
    if not name_ta:
        name_ta = translate_to_tamil(name_en)
    name_tg  = data.get('name_tanglish', row['name_tanglish'] or '').strip()
    if not name_tg and name_ta:
        name_tg = generate_tanglish_from_tamil(name_ta)
    name_d   = f"{name_en} ({name_ta})" if name_ta else name_en
    price    = float(data.get('price', data.get('price_per_kg', row['price_per_kg'])))
    emoji    = data.get('emoji', row['emoji'])
    cat      = data.get('category', row['category'])
    # Always include both Tamil script and Tanglish phonetic in search_tags
    user_tags = data.get('search_tags', row['search_tags'] or '')
    tags = ' '.join(filter(None, [name_ta, name_tg, user_tags])).strip()

    def _safe_row(column, default=None):
        try: return row[column]
        except (IndexError, KeyError): return default

    p_rate  = data.get('p_rate');        p_rate = float(p_rate) if p_rate not in (None,'') else _safe_row('p_rate')
    s_rate  = data.get('s_rate');        s_rate = float(s_rate) if s_rate not in (None,'') else _safe_row('s_rate')
    wrate   = data.get('wholesale_rate'); wrate = float(wrate)  if wrate  not in (None,'') else _safe_row('wholesale_rate')

    # Normalize nutrients
    raw_nuts = data.get('nutrients', json.loads(row['nutrients'] or '{}'))
    # ── Safe-parse every nutrient value to float ──
    for k in list(raw_nuts.keys()):
        raw_nuts[k] = get_float(raw_nuts[k])
    ntype    = data.get('nutrient_type') or _safe_row('nutrient_type') or classify_nutrient_type(raw_nuts)
    if ntype not in ('normal', 'oil'):
        ntype = classify_nutrient_type(raw_nuts)
    normalized_nuts = normalize_nutrients(raw_nuts, ntype)
    nuts = json.dumps(normalized_nuts)

    # ── serial_no (S.NO) ─────────────────────────────────────────────────────
    # Only update when caller explicitly provides a value, so we don't reset
    # canonical catalog ordering on every save.
    serial_in = data.get('serial_no', '__missing__')
    if serial_in != '__missing__':
        try:
            new_serial = int(serial_in) if serial_in not in (None, '', 'null') else None
        except (TypeError, ValueError):
            new_serial = None
        if new_serial is not None:
            conn.execute('UPDATE products SET serial_no=? WHERE id=?', (new_serial, pid))

    conn.execute('''UPDATE products
                    SET name_english=?, name_tamil=?, name_tanglish=?, name=?,
                        price=?, price_per_kg=?, emoji=?, category=?,
                        search_tags=?, nutrients=?, nutrient_type=?,
                        p_rate=?, s_rate=?, wholesale_rate=?
                    WHERE id=?''',
                 (name_en, name_ta, name_tg, name_d, price, price, emoji, cat,
                  tags, nuts, ntype, p_rate, s_rate, wrate, pid))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════
# ADMIN – Upload product image (base64)
# ═══════════════════════════════════════════════════════
@app.route('/admin/products/<pid>/image', methods=['POST'])
def upload_image(pid):
    data = request.get_json()
    if not data or not data.get('image_data'):
        return jsonify({'success': False, 'message': 'No image data'}), 400
    img_str = data['image_data']
    if ',' in img_str:
        header, img_b64 = img_str.split(',', 1)
        ext = 'png' if 'png' in header else ('gif' if 'gif' in header else 'jpg')
    else:
        img_b64, ext = img_str, 'jpg'
    # Use original filename when provided; fall back to pid-based name
    from werkzeug.utils import secure_filename as _sf
    orig_name = (data.get('original_filename') or '').strip()
    filename  = _sf(orig_name) if orig_name else ''
    if not filename:
        filename = f'{pid}.{ext}'
    if os.path.splitext(filename)[1].lower() not in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
        return jsonify({'success': False, 'message': 'Only png/jpg/gif/webp images are allowed'}), 400

    # Save to frontend/images/ — served as static files, no API proxy needed
    frontend_img_dir = os.path.join(FRONTEND_DIR, 'images')
    os.makedirs(frontend_img_dir, exist_ok=True)
    try:
        with open(os.path.join(frontend_img_dir, filename), 'wb') as f:
            f.write(base64.b64decode(img_b64))
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    conn = get_db()
    conn.execute(
        'UPDATE products SET image=?, image_path=?, image_filename=? WHERE id=?',
        (filename, filename, filename, pid)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'image': filename})


# ═══════════════════════════════════════════════════════
# ADMIN – Delete product
# ═══════════════════════════════════════════════════════
@app.route('/admin/products/<pid>', methods=['DELETE'])
def delete_product(pid):
    conn = get_db()
    conn.execute('DELETE FROM products WHERE id=?', (pid,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════
# ADMIN – Stock
# ═══════════════════════════════════════════════════════
@app.route('/admin/products/<pid>/stock', methods=['GET'])
def get_stock(pid):
    conn = get_db()
    row  = conn.execute('SELECT stock FROM products WHERE id=?', (pid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'success': False, 'message': 'Not found'}), 404
    return jsonify({'success': True, 'stock': row['stock']})

@app.route('/admin/products/<pid>/stock', methods=['PUT'])
def update_stock(pid):
    data = request.get_json()
    conn = get_db()
    row  = conn.execute('SELECT id FROM products WHERE id=?', (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': 'Not found'}), 404
    stock_val = (data or {}).get('stock')
    try:
        stock_kg = None if (stock_val is None or stock_val == '') else round(
            _finite_number(stock_val, 'stock', minimum=0), 4)
    except BillValidationError as ve:
        conn.close()
        return jsonify({'success': False, 'message': str(ve)}), 400
    conn.execute('UPDATE products SET stock=? WHERE id=?', (stock_kg, pid))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'stock': stock_kg})


# ═══════════════════════════════════════════════════════
# ADMIN – Customer search
# ═══════════════════════════════════════════════════════
@app.route('/admin/customers', methods=['GET'])
def search_customers():
    q    = request.args.get('q', '').strip()
    conn = get_db()
    if q:
        rows = conn.execute(
            "SELECT c.phone, c.name, c.stars, COALESCE(c.total_due,0) as total_due, COUNT(b.id) as bill_count "
            "FROM customers c LEFT JOIN bills b ON b.customer_phone=c.phone "
            "WHERE c.phone LIKE ? OR c.name LIKE ? "
            "GROUP BY c.phone ORDER BY c.name",
            (f'%{q}%', f'%{q}%')
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT c.phone, c.name, c.stars, COALESCE(c.total_due,0) as total_due, COUNT(b.id) as bill_count "
            "FROM customers c LEFT JOIN bills b ON b.customer_phone=c.phone "
            "GROUP BY c.phone ORDER BY c.name"
        ).fetchall()
    conn.close()
    return jsonify({'success': True, 'customers': [dict(r) for r in rows]})


@app.route('/admin/customers', methods=['POST'])
def create_customer():
    """Manually add a customer (same `customers` table used by billing lookup)."""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or data.get('customer_name') or '').strip()
    phone_raw = (data.get('phone') or data.get('customer_phone') or '').strip()
    phone = re.sub(r'\D', '', phone_raw)

    if not name:
        return jsonify({'success': False, 'error': 'Customer name is required'}), 400
    if not phone:
        return jsonify({'success': False, 'error': 'Phone number is required'}), 400
    if not phone.isdigit():
        return jsonify({'success': False, 'error': 'Phone must contain digits only'}), 400
    if len(phone) < 10 or len(phone) > 15:
        return jsonify({'success': False, 'error': 'Phone must be 10–15 digits'}), 400

    now = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    conn = get_db()
    existing = conn.execute('SELECT phone FROM customers WHERE phone=?', (phone,)).fetchone()
    if existing:
        conn.close()
        return jsonify({'success': False, 'error': 'This phone number is already registered'}), 409

    conn.execute(
        'INSERT INTO customers (phone, name, last_updated, stars, total_due, created_at) '
        'VALUES (?, ?, ?, 0, 0, ?)',
        (phone, name, now, now),
    )
    conn.commit()
    row = conn.execute('SELECT * FROM customers WHERE phone=?', (phone,)).fetchone()
    conn.close()
    return jsonify({'success': True, 'customer': dict(row), 'message': 'Customer saved'})

@app.route('/admin/customers/<phone>', methods=['DELETE'])
def delete_customer_endpoint(phone):
    conn = get_db()
    cust = conn.execute('SELECT total_due FROM customers WHERE phone=?', (phone,)).fetchone()
    if cust and round(float(cust['total_due'] or 0), 2) > 0:
        conn.close()
        # AUDIT: deleting would orphan an unpaid receivable.
        return jsonify({'success': False,
                        'message': f'Customer still owes ₹{float(cust["total_due"]):.2f}. Collect the due before deleting.'}), 409
    conn.execute('DELETE FROM customers WHERE phone=?', (phone,))
    # Optionally remove the phone from existing bills so they become walk-in bills
    conn.execute('UPDATE bills SET customer_phone = NULL WHERE customer_phone=?', (phone,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'Customer deleted'})


@app.route('/admin/customers/<phone>/bills', methods=['GET'])
def customer_bills(phone):
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM bills WHERE customer_phone=? ORDER BY ' + CREATED_AT_SORT_SQL + ' DESC, rowid DESC', (phone,)
    ).fetchall()
    conn.close()
    return jsonify({'success': True, 'bills': [row_to_dict(r) for r in rows]})


# ═══════════════════════════════════════════════════════
# CUSTOMER lookup by phone  (includes loyalty stars)
# ═══════════════════════════════════════════════════════
@app.route('/customer/<phone>', methods=['GET'])
def get_customer(phone):
    conn = get_db()
    row  = conn.execute('SELECT * FROM customers WHERE phone=?', (phone,)).fetchone()
    conn.close()
    ls = _get_loyalty_settings_data()
    if row:
        stars     = int(row['stars'] or 0)
        total_due = round(float(row['total_due'] or 0), 2)
        return jsonify({
            'success': True, 'found': True, 'name': row['name'],
            'stars': stars,
            'stars_for_reward': ls['stars_required'],
            'reward_available': stars >= ls['stars_required'],
            'reward_discount':  ls['discount'],
            'min_purchase':     ls['min_purchase'],
            'loyalty_enabled':  ls['enabled'],
            'total_due':        total_due,
            'has_due':          total_due > 0,
        })
    return jsonify({
        'success': True, 'found': False, 'stars': 0,
        'stars_for_reward': ls['stars_required'],
        'reward_available': False,
        'reward_discount':  ls['discount'],
        'min_purchase':     ls['min_purchase'],
        'loyalty_enabled':  ls['enabled'],
        'total_due':        0,
        'has_due':          False,
    })


# ═══════════════════════════════════════════════════════
# LOYALTY SETTINGS
# ═══════════════════════════════════════════════════════
@app.route('/api/loyalty-settings', methods=['GET'])
def get_loyalty_settings():
    conn = get_db()
    row  = conn.execute('SELECT * FROM loyalty_settings WHERE id=1').fetchone()
    conn.close()
    if row:
        return jsonify({'success': True, 'settings': dict(row)})
    return jsonify({'success': True, 'settings': {
        'loyalty_enabled': 1, 'min_purchase_for_star': 500,
        'stars_for_reward': 10, 'reward_discount': 100
    }})

@app.route('/api/loyalty-settings', methods=['POST'])
def save_loyalty_settings():
    data = request.get_json() or {}
    conn = get_db()
    conn.execute('''UPDATE loyalty_settings SET
        loyalty_enabled=?, min_purchase_for_star=?, stars_for_reward=?, reward_discount=?
        WHERE id=1''',
        (1 if data.get('loyalty_enabled') else 0,
         float(data.get('min_purchase_for_star', 500)),
         int(data.get('stars_for_reward', 10)),
         float(data.get('reward_discount', 100))))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════
# SHORTCUTS – Persist keyboard shortcut configuration
# Stored as JSON string in app_settings under key 'kb_shortcuts'
# ═══════════════════════════════════════════════════════
@app.route('/api/shortcuts', methods=['GET'])
def get_shortcuts():
    """Return saved shortcut map, or empty dict if not yet saved."""
    conn = get_db()
    row  = conn.execute("SELECT value FROM app_settings WHERE key='kb_shortcuts'").fetchone()
    conn.close()
    if row:
        try:
            return jsonify({'success': True, 'shortcuts': json.loads(row['value'])})
        except Exception:
            pass
    return jsonify({'success': True, 'shortcuts': {}})

@app.route('/api/shortcuts', methods=['POST'])
def save_shortcuts():
    """Save shortcut map to database so it persists across restarts."""
    data = request.get_json()
    if not data or 'shortcuts' not in data:
        return jsonify({'success': False, 'message': 'Missing shortcuts'}), 400
    shortcuts = data['shortcuts']
    if not isinstance(shortcuts, dict):
        return jsonify({'success': False, 'message': 'Invalid format'}), 400
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('kb_shortcuts', ?)",
        (json.dumps(shortcuts, ensure_ascii=False),)
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})



# ═══════════════════════════════════════════════════════
# CREDIT & DUE TRACKING  (v8)
# ═══════════════════════════════════════════════════════

@app.route('/api/customer/<phone>/collect-payment', methods=['POST'])
def collect_payment(phone):
    """Record a payment collected from a customer against their outstanding due."""
    data   = request.get_json() or {}
    amount = round(float(data.get('amount', 0)), 2)
    method = data.get('method', 'cash').strip() or 'cash'
    note   = data.get('note', '').strip()

    if amount <= 0:
        return jsonify({'success': False, 'message': 'Amount must be greater than 0'}), 400

    conn = get_db()
    cust = conn.execute('SELECT * FROM customers WHERE phone=?', (phone,)).fetchone()
    if not cust:
        conn.close()
        return jsonify({'success': False, 'message': 'Customer not found'}), 404

    current_due = round(float(cust['total_due'] or 0), 2)
    if amount > current_due + 0.01:  # allow tiny float rounding
        conn.close()
        return jsonify({
            'success': False,
            'message': f'Payment ₹{amount} exceeds outstanding due ₹{current_due}'
        }), 400

    new_due = round(max(0, current_due - amount), 2)
    now     = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

    # Deduct from customer total_due
    conn.execute('UPDATE customers SET total_due=? WHERE phone=?', (new_due, phone))

    # Record in payment_history
    conn.execute(
        'INSERT INTO payment_history (customer_id, amount, date, method, note) VALUES (?,?,?,?,?)',
        (phone, amount, now, method, note)
    )

    # Auto-apply to oldest unpaid bills first
    remaining = amount
    unpaid_bills = conn.execute(
        'SELECT id, due_amount FROM bills WHERE customer_phone=? AND payment_status=? '
        'ORDER BY ' + CREATED_AT_SORT_SQL + ' ASC, rowid ASC',
        (phone, 'CREDIT')
    ).fetchall()
    for bill_row in unpaid_bills:
        if remaining <= 0:
            break
        bill_due  = round(float(bill_row['due_amount'] or 0), 2)
        apply_amt = min(remaining, bill_due)
        new_bill_due = round(bill_due - apply_amt, 2)
        new_status   = 'PAID' if new_bill_due <= 0 else 'CREDIT'
        conn.execute(
            'UPDATE bills SET due_amount=?, payment_status=?, paid_amount=ROUND(paid_amount+?,2) WHERE id=?',
            (new_bill_due, new_status, apply_amt, bill_row['id'])
        )
        remaining = round(remaining - apply_amt, 2)

    conn.commit()
    conn.close()

    return jsonify({
        'success': True,
        'message': f'Payment of ₹{amount} recorded successfully',
        'previous_due': current_due,
        'new_due':      new_due,
        'amount_paid':  amount,
        'method':       method,
    })


@app.route('/api/customer/<phone>/payment-history', methods=['GET'])
def get_payment_history(phone):
    """Get payment history for a customer."""
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM payment_history WHERE customer_id=? ORDER BY date DESC',
        (phone,)
    ).fetchall()
    bills = conn.execute(
        'SELECT id, created_at, total, paid_amount, due_amount, payment_status, payment_method '
        'FROM bills WHERE customer_phone=? ORDER BY ' + CREATED_AT_SORT_SQL + ' DESC, rowid DESC',
        (phone,)
    ).fetchall()
    cust = conn.execute('SELECT * FROM customers WHERE phone=?', (phone,)).fetchone()
    conn.close()

    return jsonify({
        'success': True,
        'customer': dict(cust) if cust else {},
        'payment_history': [dict(r) for r in rows],
        'bills': [dict(b) for b in bills],
        'total_due': round(float(cust['total_due'] or 0), 2) if cust else 0,
    })


@app.route('/api/credit-report', methods=['GET'])
def credit_report():
    """Summary: total credit customers, total outstanding amount."""
    conn = get_db()
    rows = conn.execute(
        "SELECT c.phone, c.name, COALESCE(c.total_due,0) as total_due, "
        "COUNT(b.id) as credit_bills "
        "FROM customers c "
        "LEFT JOIN bills b ON b.customer_phone=c.phone AND b.payment_status='CREDIT' "
        "WHERE COALESCE(c.total_due,0) > 0 "
        "GROUP BY c.phone ORDER BY total_due DESC"
    ).fetchall()
    total_outstanding = conn.execute(
        "SELECT ROUND(SUM(COALESCE(total_due,0)),2) FROM customers WHERE COALESCE(total_due,0)>0"
    ).fetchone()[0] or 0
    credit_customer_count = len(rows)
    conn.close()

    return jsonify({
        'success': True,
        'credit_customers': [dict(r) for r in rows],
        'total_credit_customers': credit_customer_count,
        'total_outstanding_amount': round(float(total_outstanding), 2),
    })


# ═══════════════════════════════════════════════════════
# BILL – Save
# ═══════════════════════════════════════════════════════
PRICE_MODES = ('s_rate', 'p_rate', 'wholesale')
MAX_LINE_WEIGHT_G = 1_000_000      # 1 tonne per line — anything above is a typo
MAX_MONEY = 10_000_000


def resolve_rate_per_kg(row, price_mode):
    """Server-authoritative per-kg rate — mirrors frontend getEffectivePrice().

    AUDIT FIX F1: the POS can bill in S.Rate / P.Rate / Wholesale mode. Before
    this fix the backend always charged price_per_kg, so wholesale bills were
    saved and printed at the retail price the cashier never saw.
    """
    keys = row.keys()
    def col(name):
        return row[name] if name in keys else None
    ppk = col('price_per_kg')
    if price_mode == 'p_rate':
        rate = col('p_rate') if col('p_rate') is not None else (ppk or 100)
    elif price_mode == 'wholesale':
        rate = col('wholesale_rate') if col('wholesale_rate') is not None else (col('s_rate') or ppk or 100)
    else:
        rate = col('s_rate') if col('s_rate') is not None else (ppk or 100)
    return float(rate)


def _validate_bill_payload(data, conn):
    """Validate + normalise a /bill payload BEFORE any database write.

    Raises BillValidationError (→ HTTP 400). Returns a cleaned copy.
    """
    if not isinstance(data, dict):
        raise BillValidationError('Request body must be a JSON object')
    items = data.get('items')
    if not isinstance(items, list) or not items:
        raise BillValidationError('No items')
    if len(items) > 500:
        raise BillValidationError('Too many items in one bill')

    price_mode = str(data.get('price_mode') or 's_rate').strip().lower()
    if price_mode not in PRICE_MODES:
        raise BillValidationError(f'price_mode must be one of {PRICE_MODES}')

    clean_items = []
    unknown = []
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise BillValidationError(f'Item {idx} is not an object')
        pid = str(item.get('product_id') or '').strip()
        if not pid:
            raise BillValidationError(f'Item {idx} has no product_id')
        it = dict(item)
        it['product_id'] = pid
        if item.get('is_package'):
            if not conn.execute('SELECT 1 FROM packages WHERE id=?', (pid,)).fetchone() \
                    and not any(p['id'] == pid for p in PACKAGES):
                unknown.append(pid)
                continue
            active = item.get('active_items')
            if not isinstance(active, list) or not active:
                raise BillValidationError(f'Package item {idx} has no ingredients')
            clean_active = []
            for ai in active:
                if not isinstance(ai, dict):
                    raise BillValidationError(f'Package item {idx} has an invalid ingredient')
                ai = dict(ai)
                ai['qty_kg'] = _finite_number(ai.get('qty_kg', 0), f'Item {idx} ingredient qty_kg', minimum=0, maximum=MAX_LINE_WEIGHT_G / 1000)
                clean_active.append(ai)
            if sum(a['qty_kg'] for a in clean_active) <= 0:
                raise BillValidationError(f'Package item {idx} has zero quantity')
            it['active_items'] = clean_active
        else:
            row = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
            if not row:
                unknown.append(pid)
                continue
            it['weight_g'] = _finite_number(item.get('weight_g'), f'Item {idx} weight_g', minimum=0, maximum=MAX_LINE_WEIGHT_G, allow_equal_min=False)
            if item.get('temp_price_per_kg') is not None:
                it['temp_price_per_kg'] = _finite_number(item['temp_price_per_kg'], f'Item {idx} temp_price_per_kg', minimum=0, maximum=MAX_MONEY)
            else:
                it.pop('temp_price_per_kg', None)
            # Optional safety net: the frontend sends the rate it displayed.
            expected = item.get('expected_price_per_kg')
            if expected is not None:
                expected = _finite_number(expected, f'Item {idx} expected_price_per_kg', minimum=0)
                actual = it.get('temp_price_per_kg', resolve_rate_per_kg(row, price_mode))
                if abs(actual - expected) > 0.005:
                    raise BillValidationError(
                        f'Price for "{row["name_english"] or pid}" changed '
                        f'(screen ₹{expected:.2f}/kg, current ₹{actual:.2f}/kg). Refresh and re-check the cart.')
        clean_items.append(it)
    if unknown:
        raise BillValidationError('Unknown product(s): ' + ', '.join(unknown))

    out = dict(data)
    out['items'] = clean_items
    out['price_mode'] = price_mode
    out['cash'] = _finite_number(data.get('cash') or 0, 'cash', minimum=0, maximum=MAX_MONEY)
    if data.get('paid_amount') is not None:
        out['paid_amount'] = _finite_number(data['paid_amount'], 'paid_amount', minimum=0, maximum=MAX_MONEY)
    else:
        out.pop('paid_amount', None)
    method = str(data.get('payment_method') or 'cash').strip().lower() or 'cash'
    if method not in ('cash', 'upi', 'card', 'credit'):
        raise BillValidationError('payment_method must be cash, upi, card or credit')
    out['payment_method'] = method
    out['customer_name'] = str(data.get('customer_name') or '').strip()[:120]
    phone = normalize_phone(data.get('customer_phone'))
    if phone and not (10 <= len(phone) <= 15):
        raise BillValidationError('Customer phone must have 10–15 digits')
    out['customer_phone'] = phone
    ref = str(data.get('client_bill_ref') or '').strip()
    if len(ref) > 64:
        raise BillValidationError('client_bill_ref too long')
    out['client_bill_ref'] = ref or None
    return out


def _bill_response_from_row(row):
    """Response for an already-saved bill (idempotent retry of the same cart)."""
    try:
        items = json.loads(row['items'] or '[]')
    except Exception:
        items = []
    total = float(row['total'] or 0)
    keys = row.keys()
    discount = float(row['loyalty_discount'] or 0) if 'loyalty_discount' in keys else 0.0
    return {
        'id': row['id'], 'timestamp': row['created_at'],
        'customer_name': row['customer_name'] or '', 'customer_phone': row['customer_phone'] or '',
        'items': items, 'total': total, 'total_amount': total,
        'original_total': money(total + discount), 'loyalty_discount': discount,
        'discount_applied': discount,
        'cash': row['cash'], 'balance': row['balance'], 'bill_language': row['bill_language'],
        'paid_amount': row['paid_amount'], 'due_amount': row['due_amount'],
        'payment_status': row['payment_status'], 'payment_method': row['payment_method'],
        'is_package': any(i.get('is_package') for i in items if isinstance(i, dict)),
        'duplicate_of_existing': True,
    }


@app.route('/bill', methods=['POST'])
def save_bill():
    """Validate, then save the bill in ONE transaction (AUDIT FIX F2/F3/F6).

    * Validation happens before any write → a bad payload changes nothing.
    * BEGIN IMMEDIATE … COMMIT wraps stock, customer, loyalty, bill and due
      updates, so a failure can no longer leave stars/stock committed without
      a bill, and the connection is always closed (a leaked connection used to
      hold the write lock and make the NEXT bill fail with "database is locked").
    * client_bill_ref makes retries idempotent: re-sending the same cart after
      a failed print returns the already-saved bill instead of a duplicate.
    """
    data = request.get_json(silent=True)
    conn = get_db()
    try:
        try:
            clean = _validate_bill_payload(data, conn)
        except BillValidationError as ve:
            return jsonify({'success': False, 'message': str(ve)}), 400

        conn.execute('BEGIN IMMEDIATE')
        if clean['client_bill_ref']:
            existing = conn.execute('SELECT * FROM bills WHERE client_bill_ref=?',
                                    (clean['client_bill_ref'],)).fetchone()
            if existing:
                conn.rollback()
                app.logger.info('[Bill] Duplicate submit ignored — returning existing bill %s', existing['id'])
                return jsonify({'success': True, 'duplicate': True,
                                'bill': _bill_response_from_row(existing)}), 200
        try:
            result = _save_bill_in_txn(clean, conn)
        except BillValidationError as ve:
            conn.rollback()
            return jsonify({'success': False, 'message': str(ve)}), 400
        conn.commit()
        app.logger.info('[Bill] Saved bill %s total=%.2f paid=%.2f due=%.2f method=%s',
                        result['id'], result['total'], result['paid_amount'],
                        result['due_amount'], result['payment_method'])
        return jsonify({'success': True, 'bill': result}), 201
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        app.logger.exception('[Bill] Save failed — transaction rolled back, nothing written')
        return jsonify({'success': False, 'message': 'Bill could not be saved (database error). Nothing was recorded — please retry.'}), 500
    finally:
        conn.close()


def _save_bill_in_txn(data, conn):
    processed   = []
    total       = 0.0
    requested_lang = str(data.get('bill_language', 'en') or 'en').lower()
    lang        = 'ta' if requested_lang in ('ta', 'tamil') else 'en'

    # ── Build bill_items list for calculate_nutrition_summary() ─────────────
    # Shape: [{"product": {nutrient_type, name_english, ...}, "weight": grams}]
    # Both regular products and package sub-items are flattened into this list.
    bill_items = []

    for item in data['items']:
        if item.get('is_package'):
            pkg_id       = item.get('product_id')
            active_items = item.get('active_items', [])
            # Load package from DB
            _pkg_row  = conn.execute('SELECT * FROM packages WHERE id=?', (pkg_id,)).fetchone()
            if _pkg_row:
                try: _pkg_items = json.loads(_pkg_row['items'] or '[]')
                except: _pkg_items = []
                try: _nutr_ov = json.loads(_pkg_row['nutrition_override']) if _pkg_row['nutrition_override'] else None
                except: _nutr_ov = None
                _pkg_product_mode = (_nutr_ov or {}).get('_product_mode', 'normal')
                pkg = {
                    'id': _pkg_row['id'], 'name_english': _pkg_row['name_english'],
                    'name_tamil': _pkg_row['name_tamil'], 'emoji': _pkg_row['emoji'],
                    'price_per_kg': _pkg_row['price_per_kg'], 'items': _pkg_items,
                    'nutrition_override': _nutr_ov,
                    'product_mode': _pkg_product_mode,
                }
            else:
                pkg = next((p for p in PACKAGES if p['id'] == pkg_id), None)
            if not pkg:
                raise BillValidationError(f'Unknown package {pkg_id}')
            qty_kg     = round(sum(float(ai.get('qty_kg', 0)) for ai in active_items), 4)
            # STRICT: Customer billing ALWAYS uses package selling price_per_kg.
            # ingredient-level prices are ONLY for analytics/profit display — NEVER billing.
            # Formula: line_price = qty_kg * package.price_per_kg  (exact, half-up)
            line_price = line_amount(float(pkg['price_per_kg']), round(qty_kg * 1000, 1))
            total     += line_price

            # ── Build nutrition input list for calculate_package_nutrition ────
            # Each active_item carries its weight (qty_kg) and the nutrients
            # from the corresponding package item definition.
            pkg_nutrition_items = []
            for ai in active_items:
                ai_qty_kg = float(ai.get('qty_kg', 0))
                if ai_qty_kg <= 0:
                    continue
                # Use nutrients_per_100g from payload directly (frontend sends it).
                # Fall back to name-matching only when absent.
                nutrients_per_100g = ai.get('nutrients_per_100g') or {}
                if not nutrients_per_100g:
                    ai_name_ta = ai.get('name_tamil') or ai.get('name_ta') or ''
                    ai_name_en = (ai.get('name_english') or ai.get('name_en') or '').lower()
                    pkg_item_def = next(
                        (it for it in pkg['items']
                         if (it.get('name_tamil') or it.get('name_ta') or '') == ai_name_ta
                         or (it.get('name_english') or it.get('name_en') or '').lower() == ai_name_en),
                        None
                    )
                    if pkg_item_def:
                        nutrients_per_100g = pkg_item_def.get('nutrients_per_100g', {})

                pkg_nutrition_items.append({
                    'name_english':     ai.get('name_english') or ai.get('name_en') or '',
                    'name_tamil':       ai.get('name_tamil')   or ai.get('name_ta')  or '',
                    'weight_kg':        ai_qty_kg,
                    'nutrients_per_100g': nutrients_per_100g,
                })

                # Also feed into bill_items for legacy calculate_nutrition_summary
                ai_name  = (ai.get('name_english') or ai.get('name_en') or ai.get('name_tamil') or 'pkg-item')
                ai_ntype = 'oil' if _is_oil_ingredient(ai_name) else 'normal'
                bill_items.append({
                    'product': {
                        'nutrient_type': ai_ntype,
                        'name_english':  ai_name,
                    },
                    'weight': ai_qty_kg * 1000.0,
                })

            # ── Calculate weighted-average package nutrition ───────────────────
            # If the package has an overall nutrition override, use that directly.
            nutr_override = pkg.get('nutrition_override')
            if nutr_override and any(float(nutr_override.get(k, 0)) for k in ('protein','fat','carbohydrates')):
                pkg_nutrition = {
                    'protein':            float(nutr_override.get('protein', 0)),
                    'fat':                float(nutr_override.get('fat', 0)),
                    'carbohydrates':      float(nutr_override.get('carbohydrates', 0)),
                    'fiber':              float(nutr_override.get('fiber', 0)),
                    'moisture':           float(nutr_override.get('moisture', 0)),
                    'ash':                float(nutr_override.get('ash', 0)),
                    'absorbable_protein': float(nutr_override.get('absorbable_protein', 0)),
                }
                pkg_has_oil    = False
                pkg_has_normal = True
            else:
                pkg_nutr_result = calculate_package_nutrition(pkg_nutrition_items)
                pkg_nutrition   = pkg_nutr_result['nutrition']     # per-100g % values
                pkg_has_oil     = pkg_nutr_result['has_oil']
                pkg_has_normal  = pkg_nutr_result['has_normal']


            english_name = (pkg.get('name_english') or pkg.get('name_tamil') or '').strip()
            tamil_name = (pkg.get('name_tamil') or '').strip()
            stored_active_items = []
            for active_item in active_items:
                stored_item = dict(active_item)
                ingredient_en = (
                    active_item.get('name_english') or active_item.get('name_en') or
                    active_item.get('name') or ''
                ).strip()
                ingredient_ta = (
                    active_item.get('tamil_name') or active_item.get('name_tamil') or
                    active_item.get('name_ta') or active_item.get('tamil') or ''
                ).strip()
                stored_item.update({
                    'name': ingredient_en,
                    'name_english': ingredient_en,
                    'name_tamil': ingredient_ta,
                    'tamil_name': ingredient_ta,
                })
                stored_active_items.append(stored_item)
            _pkg_mode = pkg.get('product_mode', 'normal')
            # If product_mode is 'oil', force has_oil flag
            if _pkg_mode == 'oil':
                pkg_has_oil = True
            processed.append({
                'product_id':        pkg_id,
                'is_package':        True,
                'name':              english_name,
                'name_english':      english_name,
                'name_tamil':        tamil_name,
                'tamil_name':        tamil_name,
                'qty':               qty_kg,
                'price':             line_price,
                'unit':              'kg',
                'weight_g':          qty_kg * 1000,
                'weight_kg':         qty_kg,
                'price_per_kg':      float(pkg['price_per_kg']),
                'total_price':       line_price,
                'active_items':      stored_active_items,
                'nutrition_display': pkg_nutrition,
                'nutrition_calc':    pkg_nutrition,
                'type':              'oil' if pkg_has_oil else 'normal',
                'has_oil':           pkg_has_oil,
                'has_normal':        pkg_has_normal,
                'package_mode':      _pkg_mode,
            })
            continue

        pid  = item.get('product_id')
        row  = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
        if not row:
            raise BillValidationError(f'Unknown product {pid}')

        weight_g     = float(item['weight_g'])
        if item.get('temp_price_per_kg') is not None:
            price_per_kg = float(item['temp_price_per_kg'])
        else:
            price_per_kg = resolve_rate_per_kg(row, data['price_mode'])
        weight_kg    = round(weight_g / 1000, 4)
        line_price   = line_amount(price_per_kg, weight_g)

        # Stock is informational only — backend deducts after sale but never blocks it

        total       += line_price

        # Resolve nutrient_type from DB — still written to DB nutrient_type column
        # for backward compat with /products endpoint display.  Bill nutrition
        # itself is now computed by _calc_nutrition_engine() below.
        try:
            raw_nuts  = json.loads(row['nutrients'] or '{}')
        except Exception:
            raw_nuts  = {}
        row_ntype = (row['nutrient_type'] or '').strip()
        if row_ntype not in ('normal', 'oil', 'other'):
            row_ntype = classify_nutrient_type(raw_nuts)

        en = (row['name_english'] or '').strip()
        ta = (row['name_tamil']   or '').strip()

        # ── v20: nutrition via engine ─────────────────────────────────────────
        english_name = en or (row['name'] or '').strip() or ta
        display = (ta if lang == 'ta' else english_name) or english_name
        category_str = (row['category'] or '').strip()
        product_name = en or display

        # STEP 2 — Classify product type using nutrition_engine
        product_type = classify_product(product_name, category_str)

        # STEP 3 — TWO separate nutrition variables:
        #   base_nutrition       → per-100g % values (for DISPLAY on bill, range 0–100)
        #   calculated_nutrition → grams scaled to purchase quantity (internal only)

        # base_nutrition: per-100g percentage via calculate_nutrition_percent()
        # This is type-aware (hides moisture/ash for normal, etc.) and guaranteed 0-100.
        pct_result     = _calc_nutrition_pct(product_name, category_str)
        base_nutrition = {
            k: round(float(pct_result['nutrition'].get(k) or 0.0), 1)
            for k in STANDARD_NUTRIENT_KEYS
        }

        # ── FALLBACK: if engine returned empty, use product's stored DB nutrients ──
        if not any(v > 0 for v in base_nutrition.values()):
            db_nuts = normalize_nutrients(raw_nuts, row_ntype)
            base_nutrition = {
                k: round(float(db_nuts.get(k) or 0.0), 1)
                for k in STANDARD_NUTRIENT_KEYS
            }

        # calculated_nutrition: grams scaled to actual purchase quantity
        nutrition_result     = _calc_nutrition_engine(
            name       = product_name,
            category   = category_str,
            quantity_g = weight_g,
        )
        calculated_nutrition = nutrition_result.get('nutrition', {})

        # ── FALLBACK: if engine calc returned empty, scale from DB nutrients ──
        if not calculated_nutrition or not any(float(v) > 0 for v in calculated_nutrition.values()):
            db_nuts = normalize_nutrients(raw_nuts, row_ntype)
            calculated_nutrition = {
                k: round(float(db_nuts.get(k, 0)) * weight_g / 100.0, 1)
                for k in STANDARD_NUTRIENT_KEYS
            }

        item_type            = nutrition_result.get('type', product_type)

        # Also add to bill_items so calculate_nutrition_summary() still works.
        bill_items.append({
            'product': {
                'nutrient_type': item_type,
                'name_english':  en or row['id'],
                'category':      category_str,
                'id':            row['id'],
            },
            'weight': weight_g,
        })

        conn.execute(
            'UPDATE products SET stock = MAX(0, ROUND(stock - ?, 4)) WHERE id = ? AND stock IS NOT NULL',
            (weight_kg, pid)
        )

        processed.append({
            'product_id':         row['id'],
            'name':               english_name,
            'name_english':       english_name,
            'name_tamil':         ta,
            'tamil_name':         ta,
            'qty':                weight_kg,
            'price':              line_price,
            'unit':               'kg',
            'weight_g':           weight_g,
            'weight_kg':          weight_kg,
            'price_per_kg':       price_per_kg,
            'price_mode':         'temp' if item.get('temp_price_per_kg') is not None else data['price_mode'],
            'total_price':        line_price,
            'nutrient_type':      item_type,
            'type':               item_type,
            # STEP 4 — Both nutrition variables returned:
            'nutrition':          calculated_nutrition,   # internal / legacy compat
            'nutrition_display':  base_nutrition,         # per-100g % for bill display
            'nutrition_calc':     calculated_nutrition,   # scaled grams (internal only)
        })

    total = money(total)

    # ── SINGLE CALL: calculate_nutrition_summary is the only nutrition path ───
    nutrition_summary = calculate_nutrition_summary(bill_items)

    # All values below were validated/normalised by _validate_bill_payload().
    cash           = money(data.get('cash', 0))
    paid_amount    = money(data.get('paid_amount', cash))   # explicit paid; fall back to cash
    payment_method = data['payment_method']
    cname          = data['customer_name']
    cphone         = data['customer_phone']
    apply_reward   = bool(data.get('apply_loyalty_reward', False))
    now         = datetime.now().strftime('%d/%m/%Y %H:%M:%S')

    # ── Loyalty processing ────────────────────────────
    loyalty_discount = 0.0
    stars_earned     = 0
    loyalty_msg      = ''
    stars_before     = 0
    stars_after      = 0
    reward_applied   = False
    loyalty_data     = None

    # STEP 3 -- Load loyalty settings using existing connection (avoids second DB open)
    _ls_row = conn.execute('SELECT * FROM loyalty_settings WHERE id=1').fetchone()
    if _ls_row:
        minimum_purchase_for_star = float(_ls_row['min_purchase_for_star'])
        stars_required            = int(_ls_row['stars_for_reward'])
        reward_discount           = float(_ls_row['reward_discount'])
        loyalty_enabled           = bool(_ls_row['loyalty_enabled'])
    else:
        minimum_purchase_for_star = 500.0
        stars_required            = 5
        reward_discount           = 50.0
        loyalty_enabled           = False

    if cphone:
        # STEP 2 — Ensure customer record exists
        cust = conn.execute('SELECT * FROM customers WHERE phone=?', (cphone,)).fetchone()
        if not cust:
            conn.execute(
                "INSERT INTO customers (phone, name, last_updated, stars) VALUES (?, ?, ?, 0)",
                (cphone, cname or '', now)
            )
            cust = conn.execute('SELECT * FROM customers WHERE phone=?', (cphone,)).fetchone()

        stars_before = int(cust['stars'] or 0)

        if loyalty_enabled:
            # STEP 4 — Earn a star if bill qualifies
            if total >= minimum_purchase_for_star:
                stars_earned = 1

            stars_after = stars_before + stars_earned

            # Redeem stars ONLY when:
            #   1. Cashier explicitly clicked Apply
            #   2. Stars are enough
            #   3. Bill total meets minimum purchase threshold (can't redeem on tiny bills)
            if apply_reward and stars_after >= stars_required and total >= minimum_purchase_for_star:
                reward_applied   = True
                loyalty_discount = reward_discount
                stars_after     -= stars_required
                loyalty_msg      = f'🎉 Loyalty Reward Applied – ₹{reward_discount:.0f} Discount'
        else:
            stars_after = stars_before

        # STEP 3 — Save updated stars to DB
        conn.execute(
            'UPDATE customers SET stars=?,'
            ' name=CASE WHEN ? != "" THEN ? ELSE name END, last_updated=? WHERE phone=?',
            (stars_after, cname or '', cname or '', now, cphone)
        )

        # STEP 4 — Refetch customer so bill always uses DB-confirmed star value
        _updated_cust = conn.execute(
            'SELECT * FROM customers WHERE phone=?', (cphone,)
        ).fetchone()
        if _updated_cust is not None:
            stars_after = int(_updated_cust['stars'] or 0)

        # STEP 5 — Rebuild loyalty_data with DB-confirmed stars_after
        filled_stars      = min(stars_after, stars_required)
        stars_display_str = ('★ ' * filled_stars) + ('☆ ' * (stars_required - filled_stars))
        stars_display     = stars_display_str.strip()

        loyalty_data = {
            'stars_before':   stars_before,
            'stars_earned':   stars_earned,
            'stars_after':    stars_after,
            'reward_applied': reward_applied,
            'stars_display':  stars_display,
            'stars_required': stars_required,
        }

    # Apply loyalty discount to total
    final_total    = money(max(0, total - loyalty_discount))
    loyalty_discount = money(total - final_total)      # never more than the bill
    balance        = money(cash - final_total)

    # ── Cash short-payment guard ──────────────────────────────────────────────
    # In Cash mode the frontend sends paid_amount = total, so cash typed BELOW
    # the total used to be saved as fully PAID. Refuse it: the cashier must
    # collect the full amount or switch to Credit (which records the due).
    # cash == 0 means "no cash typed" (exact payment) and is unchanged.
    # UPI / card / credit are not affected.
    if payment_method == 'cash' and 0 < cash < final_total:
        raise BillValidationError(
            f'Cash ₹{cash:.2f} is less than the bill total ₹{final_total:.2f}. '
            f'Collect the full amount, or switch to Credit to record '
            f'₹{money(final_total - cash):.2f} as due.')

    # ── Credit & Due calculation ──────────────────────────────────────────────
    # paid_amount can't exceed the bill total (validated >= 0 already)
    paid_amount    = money(min(paid_amount, final_total))
    due_amount     = money(final_total - paid_amount)
    payment_status = 'PAID' if due_amount == 0 else 'CREDIT'
    if due_amount > 0 and not cphone:
        # AUDIT FIX F6: a due with no customer can never be collected.
        raise BillValidationError('Customer phone is required for a credit / partly-paid bill')

    bill_id = str(reserve_next_bill_no(conn))

    conn.execute(
        'INSERT INTO bills '
        '(id,customer_name,customer_phone,items,total,cash,balance,bill_language,created_at,'
        'paid_amount,due_amount,payment_status,payment_method,loyalty_discount,client_bill_ref) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (bill_id, cname, cphone, json.dumps(processed, ensure_ascii=False), final_total, cash, balance, lang, now,
         paid_amount, due_amount, payment_status, payment_method, loyalty_discount, data.get('client_bill_ref'))
    )
    logging.getLogger('billing.bill').debug(
        'Stored bill %s bilingual items=%s',
        bill_id,
        json.dumps([
            {
                'name': item.get('name', ''),
                'tamil_name': item.get('tamil_name', ''),
            }
            for item in processed
        ], ensure_ascii=False),
    )

    # ── Update customer running due balance ───────────────────────────────────
    if cphone and due_amount > 0:
        conn.execute(
            'UPDATE customers SET total_due = ROUND(COALESCE(total_due, 0) + ?, 2) WHERE phone=?',
            (due_amount, cphone)
        )

    # (commit/close are done by save_bill() — this whole function is one transaction)

    # ── Build nutrition_display: weighted per-100g % values for print template ──
    # For PACKAGES: use the package-level nutrition_display (already weighted avg %).
    # For PRODUCTS: use per-100g base_nutrition (already % values).
    # When there are multiple items, compute a simple average across items
    # (each item is treated equally — package nutrition already embeds its own weighting).
    _disp_totals   = {k: 0.0 for k in STANDARD_NUTRIENT_KEYS}
    _disp_count    = 0
    _bill_has_oil  = False
    _bill_has_norm = False

    for _item in processed:
        _nd = _item.get('nutrition_display', {})
        if _nd and any(float(_nd.get(k) or 0) > 0 for k in STANDARD_NUTRIENT_KEYS):
            _disp_count += 1
            for _k in STANDARD_NUTRIENT_KEYS:
                _disp_totals[_k] += float(_nd.get(_k) or 0.0)
        # Track oil/normal flags
        if _item.get('has_oil') or _item.get('type') == 'oil':
            _bill_has_oil = True
        else:
            _bill_has_norm = True

    if _disp_count > 1:
        _disp_avg = {k: round(_disp_totals[k] / _disp_count, 1) for k in STANDARD_NUTRIENT_KEYS}
    elif _disp_count == 1:
        _disp_avg = {k: round(_disp_totals[k], 1) for k in STANDARD_NUTRIENT_KEYS}
    else:
        _disp_avg = {k: 0.0 for k in STANDARD_NUTRIENT_KEYS}

    # Merge oil flag from nutrition_summary (legacy product path) too
    _bill_has_oil  = _bill_has_oil  or nutrition_summary.get('has_oil',    False)
    _bill_has_norm = _bill_has_norm or nutrition_summary.get('has_normal', False)

    # nutrition_summary_display: percentage-based, for bill template display only
    # FIX 3 — safety guard: ensure _disp_avg is never None
    _disp_avg = _disp_avg or {}

    # nutr_summary: single source dict passed to frontend as `nutr`
    nutr_summary = {
        'protein':    _disp_avg.get('protein',           0.0),
        'absorbable': _disp_avg.get('absorbable_protein',0.0),
        'fat':        _disp_avg.get('fat',               0.0),
        'carbs':      _disp_avg.get('carbohydrates',     0.0),
        'fiber':      _disp_avg.get('fiber',             0.0),
        'ash':        _disp_avg.get('ash',               0.0),
        'moisture':   _disp_avg.get('moisture',          0.0),
        'has_oil':    _bill_has_oil,
        'has_normal': _bill_has_norm,
    }
    # FIX 3 — safety guard: ensure nutr_summary is never None
    nutr_summary = nutr_summary or {}

    # Keep nutrition_summary_display for backward compat with any other consumers
    nutrition_summary_display = {
        'totals':     _disp_avg,
        'has_oil':    _bill_has_oil,
        'has_normal': _bill_has_norm,
    }

    bill = {
        'id': bill_id, 'timestamp': now,
        'customer_name': cname, 'customer_phone': cphone,
        'items': processed, 'total': final_total,
        'original_total': total,
        'loyalty_discount': loyalty_discount,
        'loyalty_msg': loyalty_msg,
        'cash': cash, 'balance': balance,
        'bill_language': lang,
        'nutrition_summary': nutrition_summary,
        # FIX 1 — pass nutr as single source dict (flat keys, % values, used by template)
        'nutr':          nutr_summary,
        # STEP 4 — Separate display vs internal nutrition at bill level
        'nutrition_display': nutrition_summary_display,   # % values for template
        'nutrition_calc':    nutrition_summary,           # gram totals (internal)
        'total_amount':      final_total,                 # explicit total field
        # Step 5 — top-level fields for frontend
        'stars':            stars_after,
        'stars_required':   stars_required,
        'reward_amount':    reward_discount,
        'discount_applied': loyalty_discount,
        'loyalty_enabled':  loyalty_enabled,   # send live toggle state so frontend never uses stale cache
        'is_package':       any(item.get('is_package') for item in processed),  # True when any item is a package
        # ── Credit & Due fields ──────────────────────────────────────
        'paid_amount':      paid_amount,
        'due_amount':       due_amount,
        'payment_status':   payment_status,
        'payment_method':   payment_method,
    }
    if loyalty_data:
        bill['loyalty'] = loyalty_data

    return bill


# ═══════════════════════════════════════════════════════
# BILLS  (list)
# ═══════════════════════════════════════════════════════
@app.route('/bills', methods=['GET'])
def get_bills():
    conn = get_db()
    rows = conn.execute('SELECT * FROM bills ORDER BY ' + CREATED_AT_SORT_SQL + ' DESC, rowid DESC').fetchall()
    conn.close()
    return jsonify({'success': True, 'bills': [row_to_dict(r) for r in rows], 'count': len(rows)})


# ═══════════════════════════════════════════════════════
# ISSUE 2 — CUSTOMER BILL HISTORY
# GET /customer-bills/<phone>
# Returns all bills for a mobile number with bill_number,
# date, and total_amount — structured for frontend reuse.
# ═══════════════════════════════════════════════════════
@app.route('/customer-bills/<phone>', methods=['GET'])
def get_customer_bills(phone):
    """Return all previous bills for a given mobile number.

    Response shape:
    {
      "success": true,
      "phone": "9876543210",
      "count": 3,
      "bills": [
        {
          "bill_number": "42",
          "date": "30/04/2026 14:32:10",
          "total_amount": 850.0,
          "payment_status": "PAID",
          "payment_method": "cash",
          "customer_name": "Ravi"
        },
        ...
      ]
    }
    """
    if not phone or not phone.strip():
        return jsonify({'success': False, 'message': 'Phone number is required'}), 400

    conn = get_db()
    rows = conn.execute(
        '''SELECT id, customer_name, customer_phone, total, created_at,
                  payment_status, payment_method
           FROM bills
           WHERE customer_phone = ?
           ORDER BY CAST(id AS INTEGER) DESC''',
        (phone.strip(),)
    ).fetchall()
    conn.close()

    bills = [
        {
            'bill_number':    row['id'],
            'date':           row['created_at'],
            'total_amount':   round(float(row['total'] or 0), 2),
            'payment_status': row['payment_status'] or 'PAID',
            'payment_method': row['payment_method'] or 'cash',
            'customer_name':  row['customer_name'] or '',
        }
        for row in rows
    ]

    return jsonify({
        'success': True,
        'phone':   phone.strip(),
        'count':   len(bills),
        'bills':   bills,
    })


# ═══════════════════════════════════════════════════════
# ISSUE 3 — REPEAT BILL / BILL DETAIL
# GET /bill/<bill_id>
# Returns full item list (name, qty/weight, price) so
# the frontend can pre-populate a new bill from an old one.
# ═══════════════════════════════════════════════════════
@app.route('/bill/<bill_id>', methods=['GET'])
def get_bill_by_id(bill_id):
    """Return item details for a specific bill.

    Response shape:
    {
      "success": true,
      "bill_number": "42",
      "date": "30/04/2026 14:32:10",
      "customer_name": "Ravi",
      "customer_phone": "9876543210",
      "total_amount": 850.0,
      "payment_status": "PAID",
      "items": [
        {
          "name": "Wheat",
          "name_english": "Wheat",
          "name_tamil": "கோதுமை",
          "product_id": "prod_xyz",
          "qty_kg": 1.5,
          "weight_g": 1500,
          "price_per_kg": 45.0,
          "total_price": 67.5,
          "is_package": false
        },
        ...
      ]
    }
    """
    if not bill_id or not str(bill_id).strip():
        return jsonify({'success': False, 'message': 'Bill ID is required'}), 400

    conn = get_db()
    row = conn.execute(
        'SELECT * FROM bills WHERE id = ?', (str(bill_id).strip(),)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({'success': False, 'message': f'Bill #{bill_id} not found'}), 404

    # Parse stored items JSON
    try:
        raw_items = json.loads(row['items'] or '[]')
    except (json.JSONDecodeError, TypeError):
        raw_items = []

    # Normalise each item to a consistent, frontend-friendly shape
    items = []
    for it in raw_items:
        weight_g  = float(it.get('weight_g') or 0)
        weight_kg = float(it.get('weight_kg') or (weight_g / 1000.0))
        items.append({
            'name':         it.get('name_english') or it.get('name') or '',
            'name_english': it.get('name_english') or it.get('name') or '',
            'name_tamil':   it.get('tamil_name') or it.get('name_tamil') or '',
            'tamil_name':   it.get('tamil_name') or it.get('name_tamil') or '',
            'product_id':   it.get('product_id') or '',
            'qty_kg':       round(weight_kg, 4),
            'weight_g':     round(weight_g, 2),
            'price_per_kg': round(float(it.get('price_per_kg') or 0), 2),
            'total_price':  round(float(it.get('total_price') or 0), 2),
            'is_package':   bool(it.get('is_package', False)),
            # Include active_items for packages so frontend can reconstruct selections
            'active_items': it.get('active_items', []) if it.get('is_package') else [],
        })

    return jsonify({
        'success':        True,
        'bill_number':    row['id'],
        'date':           row['created_at'],
        'customer_name':  row['customer_name'] or '',
        'customer_phone': row['customer_phone'] or '',
        'total_amount':   round(float(row['total'] or 0), 2),
        'paid_amount':    round(float(row['paid_amount'] or 0), 2),
        'due_amount':     round(float(row['due_amount'] or 0), 2),
        'payment_status': row['payment_status'] or 'PAID',
        'payment_method': row['payment_method'] or 'cash',
        'items':          items,
        'item_count':     len(items),
    })


# ═══════════════════════════════════════════════════════
# SIMPLE THERMAL PRINT BILL  (EN / TA  ×  Normal / Estimate)
# ═══════════════════════════════════════════════════════
# Renders a thermal-style printable bill (58mm/80mm) using
# Jinja templates: simple_bill_en.html / simple_bill_ta.html
#
# Query params (GET) or JSON body (POST):
#   language  : "english" | "tamil"   (default: "english")
#   bill_type : "normal"  | "estimate" (default: "normal")
#
# GET  /print-bill/<bill_id>?language=tamil&bill_type=estimate
# POST /print-bill                  (body: { bill_id, language, bill_type })
#
# Behavior:
#   - Normal   : no address, no "Estimate" label
#   - Estimate : shows shop address + "ESTIMATE" / "மதிப்பீடு"
# ═══════════════════════════════════════════════════════

# Shop info — single source of truth for thermal bill header.
SHOP_INFO = {
    'name_en':           'DHANA DHANIYA KADAI',
    'name_ta':           'தன தானிய கடை',
    # English address split into 2 lines (matches sample receipt)
    'address_line1':     'No.16 Thirumeni Nagar',
    'address_line2':     'School Road, Kolathur, Chennai-600099',
    'address_line3':     '',
    # Tamil address split into 3 lines
    'address_line1_ta':  'எண்.16 திருமேனி நகர்',
    'address_line2_ta':  'ஸ்கூல் ரோடு, கொளத்தூர், சென்னை-600099',
    'address_line3_ta':  '',
    # Backwards-compat single-line address (kept for any legacy reference)
    'address':           'No.16,Thirumeni Nagar,School Road, Kolathur,Chennai-600 099.',
    'address_ta':        'எண்.16,திருமேனி நகர்,ஸ்கூல் ரோடு, கொளத்தூர்,சென்னை-600 099.',
    'phone':             '9940116970, 9944308925',
}


def _build_print_context(bill_row, language, bill_type, paid_amount_override=None):
    """Shape the DB bill row into the dict the templates expect.

    Pure presentation — does NOT touch billing logic or DB writes.
    """
    try:
        raw_items = json.loads(bill_row['items'] or '[]')
    except Exception:
        raw_items = []

    items = []
    total_qty = 0.0
    subtotal  = 0.0
    # Aggregate nutrition (weighted average per 100g across all items)
    n_sum = {'protein': 0.0, 'absorbable_protein': 0.0, 'fat': 0.0,
             'carbs': 0.0, 'fiber': 0.0, 'moisture': 0.0, 'ash': 0.0}
    n_weight = 0.0
    # Issue 4: detect oil products using the stored type/nutrient_type field on each item.
    # Items store type="oil" or nutrient_type="oil" - NOT a name/category string match.
    is_oil_product = any(
        (it.get('type') or '').lower() == 'oil' or
        (it.get('nutrient_type') or '').lower() == 'oil' or
        (it.get('has_oil') is True)
        for it in raw_items
    )

    for it in raw_items:
        weight_g  = float(it.get('weight_g') or 0)
        weight_kg = float(it.get('weight_kg') or (weight_g / 1000.0))
        qty       = weight_kg if weight_kg > 0 else 1
        rate      = float(it.get('price_per_kg') or it.get('rate') or 0)
        amount    = float(it.get('total_price') or it.get('amount') or (qty * rate))
        total_qty += qty
        subtotal  += amount
        english_name = it.get('name_english') or it.get('name') or ''
        tamil_name = (
            it.get('tamil_name') or it.get('name_tamil') or
            it.get('name_ta') or it.get('tamil') or it.get('name') or ''
        )
        items.append({
            'name':       english_name,
            'name_ta':    tamil_name,
            'name_tamil': tamil_name,
            'tamil_name': tamil_name,
            'qty':     round(qty, 3),
            'rate':    round(rate, 2),
            'amount':  round(amount, 2),
        })

        # Per-item nutrition: use nutrition_display (pre-calculated per-100g average).
        # nutrition_display is already scaled per 100g - use directly, weighted by qty.
        # Fall back to nutrition_calc, then nutrition_per_100g only if absent.
        n = it.get('nutrition_display') or it.get('nutrition_calc') or \
            it.get('nutrition_per_100g') or {}
        if isinstance(n, dict) and n:
            w = qty if qty > 0 else 1
            n_sum['protein']            += float(n.get('protein') or 0) * w
            n_sum['absorbable_protein'] += float(n.get('absorbable_protein') or 0) * w
            n_sum['fat']                += float(n.get('fat') or 0) * w
            n_sum['carbs']              += float(n.get('carbs') or n.get('carbohydrates') or 0) * w
            n_sum['fiber']              += float(n.get('fiber') or 0) * w
            n_sum['moisture']           += float(n.get('moisture') or 0) * w
            n_sum['ash']                += float(n.get('ash') or 0) * w
            n_weight += w

    # Format date/time separately — matches sample: "1806   04:57:06 PM Date:01/05/2026"
    created_at = bill_row['created_at'] or ''
    dt = None
    for timestamp_format in ('%Y-%m-%d %H:%M:%S', '%d/%m/%Y %H:%M:%S'):
        try:
            dt = datetime.strptime(created_at, timestamp_format)
            break
        except (TypeError, ValueError):
            continue
    if dt:
        time_str = dt.strftime('%I:%M:%S %p')
        date_str = dt.strftime('%d/%m/%Y')
    else:
        time_str = ''
        date_str = created_at

    total_amt = round(float(bill_row['total'] or 0), 2)
    _keys = bill_row.keys()
    due_amt = round(float(bill_row['due_amount'] or 0), 2) if 'due_amount' in _keys else 0.0
    # AUDIT FIX F4: the stored paid amount is the source of truth. A stored 0
    # (credit bill, nothing paid) must print as 0 — the old `paid or total`
    # turned it into "fully paid".
    stored_paid = bill_row['paid_amount'] if 'paid_amount' in _keys else None
    stored_paid = round(float(stored_paid), 2) if stored_paid is not None else total_amt
    paid_amt = stored_paid
    # The UI may pass the cash actually tendered so the receipt can show change.
    # Only honoured for fully-paid bills and only when it is >= what was paid;
    # it can never make a credit bill look paid.
    if paid_amount_override is not None:
        try:
            _override = round(float(paid_amount_override), 2)
        except (TypeError, ValueError):
            _override = None
        if _override is not None and due_amt <= 0 and _override >= stored_paid:
            paid_amt = _override
    # returned = paid - total. Clamp to 0 so it never goes negative.
    returned_amt = round(max(paid_amt - total_amt, 0.0), 2)
    # AUDIT FIX F5: loyalty discount (recorded since this audit; derived for older bills)
    discount_amt = 0.0
    if 'loyalty_discount' in _keys and bill_row['loyalty_discount']:
        discount_amt = round(float(bill_row['loyalty_discount']), 2)
    elif round(subtotal - total_amt, 2) >= 0.01:
        discount_amt = round(subtotal - total_amt, 2)

    # Issue 5: customer details on header
    try:
        cust_name = (bill_row['customer_name'] or '').strip()
    except Exception:
        cust_name = ''
    try:
        cust_phone = (bill_row['customer_phone'] or '').strip()
    except Exception:
        cust_phone = ''

    bill_ctx = {
        'id':            bill_row['id'],
        'time':          time_str,
        'date':          date_str,
        'items':         items,
        'total_items':   len(items),
        'total_qty':     round(total_qty, 3),
        'subtotal':      round(subtotal, 2),
        'total':         total_amt,
        'paid':          paid_amt,
        'returned':      returned_amt,
        'due':           due_amt,
        'discount':      discount_amt,
        'customer_name': cust_name,
        'customer_phone': cust_phone,
    }

    # Nutrition — ALWAYS provide a dict so the box renders on every bill
    _nutr_keys = ('protein', 'absorbable_protein', 'fat', 'carbs', 'fiber', 'moisture', 'ash')
    if n_weight > 0:
        nutrition_ctx = {k: round(v / n_weight, 1) for k, v in n_sum.items()}
    else:
        nutrition_ctx = {k: 0.0 for k in _nutr_keys}
        try:
            nutr_raw = bill_row['nutrition']
        except Exception:
            nutr_raw = None
        if nutr_raw:
            try:
                n = json.loads(nutr_raw) if isinstance(nutr_raw, str) else nutr_raw
                nutrition_ctx = {
                    'protein':            round(float(n.get('protein') or 0), 1),
                    'absorbable_protein': round(float(n.get('absorbable_protein') or 0), 1),
                    'fat':                round(float(n.get('fat') or 0), 1),
                    'carbs':              round(float(n.get('carbs') or n.get('carbohydrates') or 0), 1),
                    'fiber':              round(float(n.get('fiber') or 0), 1),
                    'moisture':           round(float(n.get('moisture') or 0), 1),
                    'ash':                round(float(n.get('ash') or 0), 1),
                }
            except Exception:
                pass

    # Issue 8: Some legacy data stores nutrition per 1000g instead of per 100g.
    # If any value is clearly out of per-100g range (>100%), assume per-1000g and divide by 10.
    try:
        if any((nutrition_ctx.get(k) or 0) > 100 for k in _nutr_keys):
            nutrition_ctx = {k: round((nutrition_ctx.get(k) or 0) / 10.0, 1)
                             for k in _nutr_keys}
    except Exception:
        pass

    is_estimate = (bill_type or '').lower() == 'estimate'
    is_normal   = not is_estimate
    # Spec:
    #   NORMAL   → show shop address, NO "ESTIMATE" label
    #   ESTIMATE → show "ESTIMATE" / "மதிப்பீடு", NO address
    return {
        'shop':                SHOP_INFO,
        'bill':                bill_ctx,
        'nutrition':           nutrition_ctx,
        'is_estimate':         is_estimate,
        'is_normal':           is_normal,
        'show_address':        is_normal,
        'show_estimate_label': is_estimate,
        'is_oil_product':      is_oil_product,
        'language':            language,
        'bill_type':           bill_type,
    }


def _fetch_bill_row(bill_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM bills WHERE id = ?', (str(bill_id).strip(),)).fetchone()
    conn.close()
    return row


def _receipt_chars_per_line():
    """Printer setting for thermal column width (48 = Font A on 80mm)."""
    try:
        from services.printer_manager import PrinterManager
        return int(PrinterManager(get_db).load_settings().get('chars_per_line', 48))
    except Exception:
        return 48


def _attach_receipt_sections(ctx, chars_per_line=None):
    from services.receipt_formatter import ReceiptFormatter, format_receipt_lines
    fmt = ReceiptFormatter()
    lines = format_receipt_lines(ctx)
    text = '\n'.join(lines)
    ctx['formatted_lines'] = lines
    ctx['receipt_full'] = text
    # Tamil uses CSS mm width; English uses character count
    is_tamil = ctx.get('language') == 'tamil'
    ctx['receipt_chars'] = '80mm' if is_tamil else fmt._width(tamil=False)
    ctx['receipt'] = fmt.format_sections(ctx)
    ctx['receipt']['full_text'] = text
    ctx['receipt']['formatted_lines'] = lines
    # For Tamil: generate HTML-table layout via receipt_html.
    # simple_bill_ta.html and thermal_preview.html both prefer receipt_html over receipt_full.
    # receipt_html uses CSS table/flexbox which correctly handles proportional Tamil glyphs.
    # receipt_full (monospace pre) is kept as a fallback ONLY if receipt_html is unavailable.
    if is_tamil:
        ctx['receipt_html'] = fmt.format_tamil_html(ctx)

    # ── DEBUG: log PDF receipt path (browser-print / PDF save) ────────────────
    import logging as _logging
    _log = _logging.getLogger('billing.printer')
    _log.debug('PDF RECEIPT: chars_per_line=48  formatter=format_receipt_lines  lines=%d  language=%s', len(lines), ctx.get('language'))
    for ln in lines:
        _log.debug('PDF RECEIPT LINE: %r', ln)

    return ctx
def _template_exists(name: str) -> bool:
    """Return True if a template file exists in the templates directory."""
    # Try using the global template_dir
    tmpl_path = os.path.join(template_dir, name)
    if os.path.isfile(tmpl_path):
        return True
    # Try using Flask's template_folder
    try:
        tmpl_path2 = os.path.join(app.template_folder, name)
        if os.path.isfile(tmpl_path2):
            return True
    except Exception:
        pass
    # Try direct relative path
    tmpl_path3 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', name)
    if os.path.isfile(tmpl_path3):
        return True
    app.logger.error(f"Template not found. Checked: {tmpl_path}, {tmpl_path3}")
    return False



def _normalize_receipt_language(language):
    """Map UI/query language variants to the formatter's language keys."""
    return 'tamil' if str(language or '').lower() in ('ta', 'tamil') else 'english'


def _render_print_bill(bill_id, language, bill_type, paid_amount_override=None, preview=False):
    language = _normalize_receipt_language(language)
    bill_type = (bill_type or 'normal').lower()

    row = _fetch_bill_row(bill_id)
    if not row:
        return f"Bill #{bill_id} not found", 404

    ctx = _build_print_context(row, language, bill_type, paid_amount_override=paid_amount_override)
    ctx['preview'] = preview
    _attach_receipt_sections(ctx)

    if language == 'tamil':
        template = 'simple_bill_ta.html'
    else:
        template = 'simple_bill_en.html'

    return render_template(template, **ctx)


@app.route('/preview-thermal/<bill_id>', methods=['GET'])
def preview_thermal_bill(bill_id):
    """Exact-width thermal preview — English + Tamil, same reliability as print-bill."""
    language = _normalize_receipt_language(request.args.get('language', 'english'))
    bill_type = request.args.get('bill_type', 'normal')
    _pa_str = request.args.get('paid_amount')
    paid_amount_override = round(float(_pa_str), 2) if _pa_str else None
    try:
        # ── Handle sample preview (no real bill) ──
        if str(bill_id).lower() == 'sample':
            from datetime import datetime as _dt
            now = _dt.now()
            sample_ctx = {
                'shop': SHOP_INFO,
                'bill': {
                    'id': 89,
                    'time': now.strftime('%I:%M:%S %p'),
                    'date': now.strftime('%d/%m/%Y'),
                    'items': [
                        {'name': 'Bajra Pearl Millet', 'name_ta': 'பனிவரகு', 'name_tamil': 'பனிவரகு', 'tamil_name': 'பனிவரகு', 'qty': 1, 'rate': 75.0, 'amount': 75.00},
                        {'name': 'Foxtail Millet',     'name_ta': 'தினை',     'name_tamil': 'தினை',     'tamil_name': 'தினை',     'qty': 1, 'rate': 100.0, 'amount': 100.00},
                    ],
                    'total_items': 2,
                    'total_qty': 2,
                    'subtotal': 175.00,
                    'total': 175.00,
                    'paid': 175.00,
                    'returned': 0.00,
                    'due': 0.00,
                    'customer_name': 'Vishnu',
                    'customer_phone': '9962448809',
                },
                'nutrition': {'protein': 12.5, 'absorbable_protein': 8.5, 'fat': 3.2, 'carbs': 65.0, 'fiber': 8.1, 'moisture': 0.0, 'ash': 0.0},
                'is_estimate': bill_type == 'estimate',
                'is_normal': bill_type != 'estimate',
                'show_address': bill_type != 'estimate',
                'show_estimate_label': bill_type == 'estimate',
                'is_oil_product': False,
                'language': language,
                'bill_type': bill_type,
                'preview': True,
            }
            _attach_receipt_sections(sample_ctx)
            return render_template('thermal_preview.html', **sample_ctx)

        app.logger.info(
            f"Thermal preview request: bill_id={bill_id}, "
            f"language={language}, bill_type={bill_type}"
        )
        row = _fetch_bill_row(bill_id)
        if not row:
            return f"Bill #{bill_id} not found", 404
        ctx = _build_print_context(
            row, language, bill_type,
            paid_amount_override=paid_amount_override,
        )
        ctx['preview'] = True
        _attach_receipt_sections(ctx)
        return render_template('thermal_preview.html', **ctx)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        app.logger.error(f"Error in preview_thermal_bill: {e}\n{tb}")
        return (
            f"<h1>Thermal Preview Error</h1>"
            f"<p><b>{type(e).__name__}:</b> {e}</p>"
            f"<hr><pre>{tb}</pre>"
            f"<hr>"
            f"<p>Template folder: {app.template_folder}</p>"
            f"<p>thermal_preview.html on disk: "
            f"{os.path.isfile(os.path.join(app.template_folder, 'thermal_preview.html'))}</p>"
        ), 500


@app.route('/print-bill/<bill_id>', methods=['GET'])
def print_bill_get(bill_id):
    language  = request.args.get('language',  'english')
    bill_type = request.args.get('bill_type', 'normal')
    _pa_str   = request.args.get('paid_amount')
    paid_amount_override = round(float(_pa_str), 2) if _pa_str else None
    preview = request.args.get('preview', '').lower() in ('1', 'true', 'yes')
    return _render_print_bill(
        bill_id, language, bill_type,
        paid_amount_override=paid_amount_override,
        preview=preview,
    )


@app.route('/print-bill', methods=['POST'])
def print_bill_post():
    data      = request.get_json(silent=True) or {}
    bill_id   = data.get('bill_id') or request.args.get('bill_id')
    language  = data.get('language',  'english')
    bill_type = data.get('bill_type', 'normal')
    if not bill_id:
        return jsonify({'success': False, 'message': 'bill_id required'}), 400
    return _render_print_bill(bill_id, language, bill_type)


# ═══════════════════════════════════════════════════════
# ANALYTICS
# ═══════════════════════════════════════════════════════
@app.route('/summary', methods=['GET'])
def get_summary():
    conn  = get_db()
    today = datetime.now().strftime('%d/%m/%Y')

    # ── Respect analytics cutoff (set by /api/admin/clear-analytics) ──────
    cutoff_row = conn.execute(
        "SELECT value FROM app_settings WHERE key=?", (ANALYTICS_CUTOFF_KEY,)
    ).fetchone()
    cutoff_ts = cutoff_row['value'] if cutoff_row else None

    if cutoff_ts:
        # cutoff_ts may be in 'DD/MM/YYYY HH:MM:SS' or 'YYYY-MM-DD HH:MM:SS'
        # bills.created_at is stored as 'DD/MM/YYYY HH:MM:SS'
        # Simple string comparison works when format is consistent.
        # Convert ISO cutoff to DD/MM/YYYY format for comparison if needed.
        # AUDIT FIX F8: compare in sortable ISO form (the old string compare on
        # 'DD/MM/YYYY' ordered by day-of-month and mixed up months).
        cutoff_iso = None
        for _fmt in ('%Y-%m-%d %H:%M:%S', '%d/%m/%Y %H:%M:%S'):
            try:
                cutoff_iso = datetime.strptime(cutoff_ts, _fmt).strftime('%Y-%m-%d %H:%M:%S')
                break
            except (TypeError, ValueError):
                continue
        if cutoff_iso is None:
            app.logger.error('[Summary] Unparseable analytics cutoff %r — ignoring it', cutoff_ts)
            all_bills = conn.execute('SELECT total, items, created_at FROM bills').fetchall()
        else:
            all_bills = conn.execute(
                'SELECT total, items, created_at FROM bills WHERE ' + CREATED_AT_SORT_SQL + ' > ?',
                (cutoff_iso,)
            ).fetchall()
    else:
        all_bills = conn.execute('SELECT total, items, created_at FROM bills').fetchall()
    # ── END cutoff logic ───────────────────────────────────────────────────

    total_revenue = 0.0; today_revenue = 0.0; today_bills = 0
    product_sales = {}
    for bill in all_bills:
        total_revenue += bill['total']
        if bill['created_at'].startswith(today):
            today_revenue += bill['total']; today_bills += 1
        try: items = json.loads(bill['items'])
        except: items = []
        for item in items:
            n = item.get('name_english') or item.get('name_en') or item.get('name', 'Unknown')
            if n not in product_sales:
                product_sales[n] = {'weight_kg': 0.0, 'revenue': 0.0, 'qty': 0}
            product_sales[n]['weight_kg'] += float(item.get('weight_kg', 0))
            product_sales[n]['revenue']   += float(item.get('total_price', 0))
            product_sales[n]['qty']       += 1
    most_sold = max(product_sales, key=lambda k: product_sales[k]['revenue']) if product_sales else None
    conn.close()

    analytics_cutoff_active = cutoff_ts is not None
    return jsonify({'success': True, 'total_revenue': round(total_revenue, 2),
                    'today_revenue': round(today_revenue, 2), 'today_bills': today_bills,
                    'total_bills': len(all_bills), 'most_sold_product': most_sold,
                    'product_sales': product_sales,
                    'analytics_cutoff_active': analytics_cutoff_active,
                    'analytics_cutoff_ts': cutoff_ts})


@app.route('/summary/by-date', methods=['GET'])
def get_summary_by_date():
    """Return total bills count and total revenue for a specific date.
    Query param: date=DD/MM/YYYY  (defaults to today)
    """
    date_str = request.args.get('date', datetime.now().strftime('%d/%m/%Y')).strip()
    conn = get_db()
    all_bills = conn.execute(
        'SELECT id, total, items, customer_name, customer_phone, created_at '
        'FROM bills WHERE created_at LIKE ?', 
        (date_str + '%',)
    ).fetchall()
    conn.close()
    day_revenue = 0.0
    day_bills = []
    product_sales = {}
    for bill in all_bills:
        day_revenue += bill['total']
        day_bills.append({
            'id': bill['id'],
            'customer_name': bill['customer_name'] or '',
            'customer_phone': bill['customer_phone'] or '',
            'total': bill['total'],
            'created_at': bill['created_at'],
        })
        try: items = json.loads(bill['items'])
        except: items = []
        for item in items:
            n = item.get('name_english') or item.get('name_en') or item.get('name', 'Unknown')
            if n not in product_sales:
                product_sales[n] = {'weight_kg': 0.0, 'revenue': 0.0, 'qty': 0}
            product_sales[n]['weight_kg'] += float(item.get('weight_kg', 0))
            product_sales[n]['revenue']   += float(item.get('total_price', 0))
            product_sales[n]['qty']       += 1
    top_products = sorted(product_sales.items(), key=lambda x: x[1]['revenue'], reverse=True)[:10]
    return jsonify({
        'success': True,
        'date': date_str,
        'total_bills': len(day_bills),
        'total_revenue': round(day_revenue, 2),
        'bills': day_bills,
        'top_products': [{'name': k, **v} for k, v in top_products],
    })


# ═══════════════════════════════════════════════════════
# ADMIN – Download / Export
# ═══════════════════════════════════════════════════════
@app.route('/admin/download-db', methods=['GET'])
def download_db():
    if request.args.get('local') == '1':
        import shutil
        downloads_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
        os.makedirs(downloads_dir, exist_ok=True)
        save_path = os.path.join(downloads_dir, 'billing_backup.db')
        _sqlite_snapshot(save_path)      # online-backup API: consistent while running
        return jsonify({'success': True, 'path': save_path})
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), 'billing_download_snapshot.db')
    _sqlite_snapshot(tmp)
    return send_file(tmp, as_attachment=True, download_name='billing_backup.db')

@app.route('/admin/export/bills', methods=['GET'])
def export_bills_excel():
    from datetime import datetime
    conn = get_db()
    rows = conn.execute(
        'SELECT id,customer_name,customer_phone,total,cash,balance,created_at '
        'FROM bills ORDER BY ' + CREATED_AT_SORT_SQL + ' DESC, rowid DESC').fetchall()
    conn.close()
    wb = Workbook()
    ws = wb.active
    ws.title = "Bills"
    headers = ["Bill ID", "Customer", "Phone", "Total", "Cash", "Balance", "Change", "Date"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')
    for r in rows:
        name = r['customer_name'] if r['customer_name'] else 'Walk-in Customer'
        phone = str(r['customer_phone']).split('.')[0] if r['customer_phone'] else ''
        total = round(float(r['total'] or 0), 2)
        cash = round(float(r['cash'] or 0), 2)
        raw_balance = total - cash
        if raw_balance < 0:
            balance_due = 0.00
            change_due = round(abs(raw_balance), 2)
        else:
            balance_due = round(raw_balance, 2)
            change_due = 0.00
        created = r['created_at'] or ''
        try:
            dt = datetime.strptime(created, '%Y-%m-%d %H:%M:%S')
            created = dt.strftime('%d-%m-%Y %I:%M %p')
        except Exception:
            pass
        row_idx = ws.max_row + 1
        ws.cell(row=row_idx, column=1, value=str(r['id'])).number_format = '@'
        ws.cell(row=row_idx, column=2, value=name)
        ws.cell(row=row_idx, column=3, value=phone)
        ws.cell(row=row_idx, column=4, value=total)
        ws.cell(row=row_idx, column=5, value=cash)
        ws.cell(row=row_idx, column=6, value=balance_due)
        ws.cell(row=row_idx, column=7, value=change_due)
        ws.cell(row=row_idx, column=8, value=created)
    # Auto column width
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = max_length + 2
    # Center align data rows
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(horizontal='center')
    # Force phone column as text
    for row in ws.iter_rows(min_row=2, min_col=3, max_col=3):
        for cell in row:
            cell.number_format = '@'
            
    if request.args.get('local') == '1':
        downloads_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
        os.makedirs(downloads_dir, exist_ok=True)
        save_path = os.path.join(downloads_dir, 'bills_export.xlsx')
        wb.save(save_path)
        return jsonify({'success': True, 'path': save_path})

    file_stream = io.BytesIO()
    wb.save(file_stream)
    file_stream.seek(0)
    return send_file(file_stream, as_attachment=True, download_name='bills_export.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.route('/admin/export/products', methods=['GET'])
def export_products_excel():
    conn = get_db()
    rows = conn.execute(
        'SELECT serial_no, id, name_english, name_tamil, name_tanglish, price_per_kg, stock, category, nutrients '
        'FROM products '
        'ORDER BY CASE WHEN serial_no IS NULL THEN 1 ELSE 0 END, '
        '         serial_no ASC, '
        '         LOWER(COALESCE(name_english, name)) ASC').fetchall()
    conn.close()
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    headers = ['S.NO', 'ID', 'Name (English)', 'Name (Tamil)', 'Name (Tanglish)', 'Price/Kg', 'Stock', 'Category',
               'Protein (g)', 'Absorbable Protein (g)', 'Fat (g)', 'Carbohydrates (g)',
               'Fiber (g)', 'Moisture (g)', 'Ash (g)']
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal='center')
    import json as _json
    for r in rows:
        try:
            nuts = _json.loads(r['nutrients'] or '{}') if isinstance(r['nutrients'], str) else (r['nutrients'] or {})
        except Exception:
            nuts = {}
        ws.append([r['serial_no'], r['id'], r['name_english'], r['name_tamil'], r['name_tanglish'],
                   r['price_per_kg'],
                   r['stock'] if r['stock'] is not None else '',
                   r['category'],
                   nuts.get('protein', 0) or 0,
                   nuts.get('absorbable_protein', 0) or 0,
                   nuts.get('fat', 0) or 0,
                   nuts.get('carbohydrates', 0) or 0,
                   nuts.get('fiber', 0) or 0,
                   nuts.get('moisture', 0) or 0,
                   nuts.get('ash', 0) or 0])
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = max_length + 2
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(horizontal='center')
            
    if request.args.get('local') == '1':
        downloads_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
        os.makedirs(downloads_dir, exist_ok=True)
        save_path = os.path.join(downloads_dir, 'products_export.xlsx')
        wb.save(save_path)
        return jsonify({'success': True, 'path': save_path})

    file_stream = io.BytesIO()
    wb.save(file_stream)
    file_stream.seek(0)
    return send_file(file_stream, as_attachment=True, download_name='products_export.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'shop': 'DHANA DHANYA KADAI', 'version': '20.1', 'server': 'ready'})


# ═══════════════════════════════════════════════════════
# SERVE FRONTEND
# ═══════════════════════════════════════════════════════
@app.route('/')
def serve_index():
    """Main dashboard entry point. Redirect to login if not authenticated."""
    if not session.get('logged_in'):
        return redirect('/login')
    return send_from_directory(FRONTEND_DIR, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    """Serve static frontend assets. Auth not required for assets."""
    return send_from_directory(FRONTEND_DIR, path)


# ═══════════════════════════════════════════════════════
# LIVE PRODUCT CATALOG  (read-only, no login required)
# ═══════════════════════════════════════════════════════

def _get_catalog_products():
    """
    Fetch all active products for the public catalog.
    READ-ONLY – single SELECT, no joins, no writes.
    Uses existing get_db() / DB_PATH – single source of truth.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT
                id, name, name_english, name_tamil, name_tanglish,
                price, s_rate, price_per_kg,
                category, emoji, image, image_path, image_filename,
                search_tags, stock
            FROM products
            WHERE category != 'Miscellaneous / Internal'
               OR category IS NULL
            ORDER BY category, serial_no, name
            """
        ).fetchall()
        # Convert sqlite3.Row → plain dict for Jinja
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.route('/open-catalog')
def open_catalog_redirect():
    """
    Legacy convenience redirect — now points to the printable/PDF catalog
    to match the Open_Catalog_PDF.bat behaviour.
    """
    import webbrowser
    webbrowser.open('http://localhost:5000/catalog/print')
    return '<script>window.open("http://localhost:5000/catalog/print","_blank"); history.back();</script>'


@app.route('/catalog')
def catalog():
    """
    Public live product catalog.
    No login required – safe for sharing with customers.
    Prices reflect billing DB in real time.
    """
    products = _get_catalog_products()
    return render_template('catalog.html', products=products)



@app.route('/catalog/pdf')
def catalog_pdf():
    """
    Generate and serve a PDF of the product catalog using reportlab.
    No external dependencies beyond reportlab (pure Python, no GTK/cairo needed).
    """
    try:
        import io
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, KeepTogether
        )
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from flask import make_response
        import os

        # ── Font registration (Unicode / Tamil support) ──────────────────────
        # Priority 1: bundled fonts shipped inside backend/fonts/ (works on any OS)
        # Priority 2: system font search (fallback)
        FONT_NAME = 'CatalogFont'
        FONT_BOLD = 'CatalogFontBold'
        _registered = False

        _app_dir   = os.path.dirname(os.path.abspath(__file__))
        _fonts_dir = os.path.join(_app_dir, 'fonts')
        _bundled_reg  = os.path.join(_fonts_dir, 'FreeSerif.ttf')
        _bundled_bold = os.path.join(_fonts_dir, 'FreeSerifBold.ttf')

        if os.path.exists(_bundled_reg):
            try:
                pdfmetrics.registerFont(TTFont(FONT_NAME, _bundled_reg))
                if os.path.exists(_bundled_bold):
                    pdfmetrics.registerFont(TTFont(FONT_BOLD, _bundled_bold))
                else:
                    FONT_BOLD = FONT_NAME
                _registered = True
            except Exception:
                pass

        if not _registered:
            FONT_DIRS = [
                '/usr/share/fonts/truetype/freefont',
                '/usr/share/fonts/truetype/noto',
                '/usr/share/fonts/truetype/dejavu',
                '/usr/share/fonts',
                'C:/Windows/Fonts',
            ]
            FONT_CANDIDATES = [
                ('FreeSerif.ttf',             'FreeSerifBold.ttf'),
                ('NotoSansTamil-Regular.ttf', 'NotoSansTamil-Bold.ttf'),
                ('NotoSerif-Regular.ttf',     'NotoSerif-Bold.ttf'),
                ('DejaVuSerif.ttf',           'DejaVuSerif-Bold.ttf'),
                ('DejaVuSans.ttf',            'DejaVuSans-Bold.ttf'),
            ]
            for font_dir in FONT_DIRS:
                if _registered:
                    break
                for reg_name, bold_name in FONT_CANDIDATES:
                    reg_path  = os.path.join(font_dir, reg_name)
                    bold_path = os.path.join(font_dir, bold_name)
                    if os.path.exists(reg_path):
                        try:
                            pdfmetrics.registerFont(TTFont(FONT_NAME, reg_path))
                            if os.path.exists(bold_path):
                                pdfmetrics.registerFont(TTFont(FONT_BOLD, bold_path))
                            else:
                                FONT_BOLD = FONT_NAME
                            _registered = True
                            break
                        except Exception:
                            continue

        if not _registered:
            FONT_NAME = FONT_BOLD = 'Helvetica'  # last resort (no Tamil)

        # ── Styles ────────────────────────────────────────────────────────────
        GREEN  = colors.HexColor('#2e7d32')
        LGREY  = colors.HexColor('#f5f5f5')
        DGREY  = colors.HexColor('#555555')
        GOLD   = colors.HexColor('#f9a825')
        WHITE  = colors.white

        title_style = ParagraphStyle(
            'Title', fontName=FONT_BOLD, fontSize=22,
            textColor=WHITE, alignment=TA_CENTER, spaceAfter=2
        )
        subtitle_style = ParagraphStyle(
            'Subtitle', fontName=FONT_NAME, fontSize=10,
            textColor=WHITE, alignment=TA_CENTER
        )
        cat_style = ParagraphStyle(
            'Category', fontName=FONT_BOLD, fontSize=13,
            textColor=WHITE, alignment=TA_LEFT,
            leftIndent=4, spaceAfter=0
        )
        prod_name_style = ParagraphStyle(
            'ProdName', fontName=FONT_BOLD, fontSize=9,
            textColor=colors.HexColor('#1a237e'), leading=11
        )
        prod_eng_style = ParagraphStyle(
            'ProdEng', fontName=FONT_NAME, fontSize=7.5,
            textColor=DGREY, leading=10
        )
        price_style = ParagraphStyle(
            'Price', fontName=FONT_BOLD, fontSize=10,
            textColor=GREEN
        )
        oos_style = ParagraphStyle(
            'OOS', fontName=FONT_NAME, fontSize=7,
            textColor=colors.HexColor('#c62828')
        )

        # ── Data ──────────────────────────────────────────────────────────────
        products = _get_catalog_products()

        # Group by category
        from collections import OrderedDict
        cat_groups = OrderedDict()
        for p in products:
            cat = p.get('category') or 'Other'
            cat_groups.setdefault(cat, []).append(p)

        # ── Build PDF ─────────────────────────────────────────────────────────
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            leftMargin=12*mm, rightMargin=12*mm,
            topMargin=14*mm, bottomMargin=14*mm
        )

        W = A4[0] - 24*mm   # usable page width
        COL = 4             # products per row
        col_w = W / COL

        story = []

        # ── Header banner ─────────────────────────────────────────────────────
        header_tbl = Table([[Paragraph(
            '<b>🌾 Dhana Dhanya Kadai</b><br/>'
            '<font size="9">விலை நேரடி பட்டியல் · Live Price Catalog</font>',
            ParagraphStyle('H', fontName=FONT_BOLD, fontSize=18,
                           textColor=WHITE, alignment=TA_CENTER, leading=24)
        )]], colWidths=[W])
        header_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,-1), GREEN),
            ('TOPPADDING',    (0,0), (-1,-1), 10),
            ('BOTTOMPADDING', (0,0), (-1,-1), 10),
            ('ALIGN',         (0,0), (-1,-1), 'CENTER'),
        ]))
        story.append(header_tbl)
        story.append(Spacer(1, 4*mm))

        # ── Product count line ────────────────────────────────────────────────
        story.append(Paragraph(
            f'<font color="#555555">{len(products)} products</font>',
            ParagraphStyle('cnt', fontName=FONT_NAME, fontSize=8, alignment=TA_LEFT)
        ))
        story.append(Spacer(1, 2*mm))

        def _fmt_price(p):
            price = p.get('price') or p.get('s_rate') or p.get('price_per_kg')
            try:
                price = float(price)
                return f'₹{price:,.0f}/kg'
            except (TypeError, ValueError):
                return '—'

        def _is_oos(p):
            stk = p.get('stock')
            if stk is None:
                return False
            try:
                return float(stk) <= 0
            except (TypeError, ValueError):
                return str(stk).strip().lower() in ('0', 'out of stock', 'oos')

        # ── Categories ────────────────────────────────────────────────────────
        for cat_name, prods in cat_groups.items():
            # Category header row
            cat_hdr = Table(
                [[Paragraph(cat_name, cat_style)]],
                colWidths=[W]
            )
            cat_hdr.setStyle(TableStyle([
                ('BACKGROUND',    (0,0), (-1,-1), GREEN),
                ('TOPPADDING',    (0,0), (-1,-1), 5),
                ('BOTTOMPADDING', (0,0), (-1,-1), 5),
                ('LEFTPADDING',   (0,0), (-1,-1), 8),
            ]))
            story.append(Spacer(1, 3*mm))
            story.append(cat_hdr)
            story.append(Spacer(1, 1*mm))

            # Build grid rows (COL products per row)
            rows = []
            row = []
            for idx, p in enumerate(prods):
                tamil  = p.get('name_tamil') or p.get('name') or ''
                eng    = p.get('name_english') or ''
                price  = _fmt_price(p)
                oos    = _is_oos(p)

                cell_parts = [Paragraph(tamil, prod_name_style)]
                if eng:
                    cell_parts.append(Paragraph(eng, prod_eng_style))
                cell_parts.append(Spacer(1, 1*mm))
                cell_parts.append(Paragraph(price, price_style))
                if oos:
                    cell_parts.append(Paragraph('Out of Stock', oos_style))

                row.append(cell_parts)
                if len(row) == COL:
                    rows.append(row)
                    row = []

            # Pad last row
            while len(row) > 0 and len(row) < COL:
                row.append('')
            if row:
                rows.append(row)

            if rows:
                grid = Table(rows, colWidths=[col_w]*COL, repeatRows=0)
                grid.setStyle(TableStyle([
                    ('VALIGN',        (0,0), (-1,-1), 'TOP'),
                    ('TOPPADDING',    (0,0), (-1,-1), 5),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 5),
                    ('LEFTPADDING',   (0,0), (-1,-1), 5),
                    ('RIGHTPADDING',  (0,0), (-1,-1), 5),
                    ('ROWBACKGROUNDS',(0,0), (-1,-1), [WHITE, LGREY]),
                    ('GRID',          (0,0), (-1,-1), 0.3, colors.HexColor('#dddddd')),
                ]))
                story.append(grid)

        # ── Footer ────────────────────────────────────────────────────────────
        story.append(Spacer(1, 6*mm))
        story.append(HRFlowable(width=W, color=GREEN, thickness=1))
        story.append(Spacer(1, 2*mm))
        from datetime import datetime
        story.append(Paragraph(
            f'Generated on {datetime.now().strftime("%d %b %Y, %I:%M %p")} · Dhana Dhanya Kadai',
            ParagraphStyle('footer', fontName=FONT_NAME, fontSize=7,
                           textColor=DGREY, alignment=TA_CENTER)
        ))

        doc.build(story)
        pdf_bytes = buf.getvalue()

        response = make_response(pdf_bytes)
        response.headers['Content-Type']        = 'application/pdf'
        response.headers['Content-Disposition'] = 'attachment; filename=product_catalog.pdf'
        return response

    except Exception as e:
        import traceback
        app.logger.error(f'PDF generation failed: {traceback.format_exc()}')
        return jsonify({'error': f'PDF generation failed: {str(e)}'}), 500

@app.route('/catalog/print')
def catalog_print():
    """
    Print-friendly / PDF version of the catalog.
    No login required.
    """
    products = _get_catalog_products()
    return render_template('catalog_print.html', products=products)


@app.route('/catalog/api')
def catalog_api():
    """
    JSON endpoint – useful for embedding or WhatsApp-bot integrations.
    No login required.
    """
    products = _get_catalog_products()
    return jsonify({
        'count': len(products),
        'products': products
    })


# ─── Allow catalog routes without login ──────────────────────────────────────
# We patch check_login (defined earlier) by adding catalog endpoints to the
# allowed set.  This is done by wrapping the before_request registry so we
# don't touch the original function.

_CATALOG_PATHS = {'/catalog', '/catalog/', '/catalog/print', '/catalog/api'}

_original_check_login = app.before_request_funcs[None][-1]   # last registered

def _catalog_aware_check_login():
    if request.path.rstrip('/') in ('/catalog', '/catalog/print', '/catalog/api', '/catalog/pdf', '/open-catalog'):
        return None   # allow without auth
    return _original_check_login()

# Replace the last before_request handler with our wrapper
app.before_request_funcs[None][-1] = _catalog_aware_check_login

# ═══════════════════════════════════════════════════════
# THERMAL PRINTER API (ESC/POS direct print)
# ═══════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO)
try:
    from printer_routes import register_printer_routes
    register_printer_routes(app, get_db, _build_print_context, _fetch_bill_row)
except Exception as _printer_import_err:
    logging.getLogger('billing.printer').warning(
        'Printer routes not loaded: %s', _printer_import_err
    )

_init_app_settings()


# ═══════════════════════════════════════════════════════════════════════
# ADMIN — CLEAR ANALYTICS DATA
# POST /api/admin/clear-analytics
#
# Body (JSON):
#   {
#     "mode":        "today" | "all",   // which bills to wipe analytics for
#     "clear_bills": false | true        // whether to also DELETE bill rows
#   }
#
# SAFE TABLES — never touched:
#   products, packages, customers, customers_clean, customers_migrated,
#   loyalty_settings, app_settings, payment_history, stock columns
#
# What this route CAN do:
#   mode="today" + clear_bills=false  → response contains zeroed summary only
#                                       (analytics are computed live; nothing
#                                        is stored in a separate cache table,
#                                        so we mark a "today cutoff" setting)
#   mode="today" + clear_bills=true   → DELETE bills created today
#   mode="all"   + clear_bills=false  → (same as today/false — all aggregates
#                                        are recomputed live from bills table)
#   mode="all"   + clear_bills=true   → DELETE ALL bill rows (hard reset)
#
# Because analytics are computed on-the-fly from the bills table, the
# quickest/safest "reset without deleting bills" is to store a cutoff
# timestamp in app_settings so /summary ignores rows before it.
# ═══════════════════════════════════════════════════════════════════════

ANALYTICS_CUTOFF_KEY = 'analytics_cutoff_ts'

@app.route('/api/admin/clear-analytics', methods=['POST'])
@require_login
def clear_analytics():
    data        = request.get_json(force=True) or {}
    mode        = data.get('mode', 'all')          # "today" | "all"
    clear_bills = bool(data.get('clear_bills', False))

    if mode not in ('today', 'all', 'restore'):
        return jsonify({'success': False, 'message': 'mode must be "today", "all", or "restore"'}), 400

    conn    = get_db()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    today   = datetime.now().strftime('%d/%m/%Y')
    deleted = 0

    try:
        if mode == 'restore':
            # Remove cutoff entirely — restore full historical view
            conn.execute("DELETE FROM app_settings WHERE key=?", (ANALYTICS_CUTOFF_KEY,))
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': 'Full analytics view restored.', 'mode': 'restore'})

        if clear_bills:
            # Safety net: snapshot the DB before deleting bills.
            if not _run_backup(tag='pre_clear'):
                conn.close()
                return jsonify({'success': False,
                                'message': 'Backup before deleting bills failed — nothing was deleted.'}), 500
            if mode == 'today':
                # Delete only bills created today (created_at starts with today's date)
                cur = conn.execute(
                    "DELETE FROM bills WHERE created_at LIKE ?",
                    (f'{today}%',)
                )
            else:
                # Hard wipe — all bills
                cur = conn.execute("DELETE FROM bills")
            deleted = cur.rowcount

            # Remove analytics cutoff if we just wiped everything
            conn.execute(
                "DELETE FROM app_settings WHERE key=?",
                (ANALYTICS_CUTOFF_KEY,)
            )
        else:
            # Soft reset: store a cutoff timestamp so /summary ignores older data
            if mode == 'today':
                # Cutoff = start of today (exclude today's bills from totals)
                cutoff = datetime.now().strftime('%d/%m/%Y 00:00:00')
            else:
                # Cutoff = right now (exclude ALL past bills)
                cutoff = now_str
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
                (ANALYTICS_CUTOFF_KEY, cutoff)
            )

        conn.commit()
        conn.close()

        label = 'today' if mode == 'today' else 'all time'
        if clear_bills:
            msg = f'Deleted {deleted} bill(s) [{label}]. Analytics reset.'
        else:
            msg = f'Analytics view reset for [{label}]. Bills preserved.'

        return jsonify({
            'success':      True,
            'message':      msg,
            'mode':         mode,
            'clear_bills':  clear_bills,
            'deleted_rows': deleted,
            'timestamp':    now_str,
        })

    except Exception as e:
        conn.close()
        app.logger.error(f'clear-analytics error: {e}')
        return jsonify({'success': False, 'message': str(e)}), 500


if __name__ == '__main__':
    init_db()
    start_backup_scheduler(interval_hours=1)
    print('🌾  DHANA DHANYA KADAI Billing API v20.2  →  http://localhost:5000')
    print('    Login at: http://localhost:5000/login')
    print('    Health:   http://localhost:5000/health')
    print("="*50)
    print("Detected Templates:", app.jinja_env.list_templates())
    print("="*50)
    app.run(debug=False, use_reloader=False, port=5000)
