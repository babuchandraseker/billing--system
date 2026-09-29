"""
Post-build verification for the PyInstaller bundle in dist/DhanaDhanyaKadai.

WHY THIS FILE EXISTS
─────────────────────
desktop_app.spec was changed to bundle uharfbuzz/freetype binaries, the
backend/fonts/*.ttf files, and the lazily-imported tamil_text_render module
(see comments in desktop_app.spec). PyInstaller can silently produce a
"working" EXE even when one of these is missing -- the app still launches
fine, and the breakage only shows up the first time someone prints a Tamil
bill: either a RuntimeError ("Tamil thermal printing requires a Tamil
font") on a machine without Nirmala/Latha installed, or scrambled vowel
signs if uharfbuzz/freetype failed to bundle and something upstream
swallowed the import error.

This script inspects an already-built dist/ folder and fails loudly,
listing exactly what's missing, instead of letting that surface later on a
customer's PC.

USAGE
─────
    python verify_build.py                       # checks dist/DhanaDhanyaKadai
    python verify_build.py path/to/dist/folder    # checks a specific folder
"""

import os
import sys

DEFAULT_DIST_DIR = os.path.join('dist', 'DhanaDhanyaKadai')

REQUIRED_FONTS = [
    'NotoSansTamil-Regular.ttf',
    'NotoSansTamil-Bold.ttf',
]

# Substrings to look for among bundled filenames (covers .pyd/.dll/.so
# names, which vary by platform and PyInstaller version).
REQUIRED_NATIVE_LIB_HINTS = [
    'uharfbuzz',
    'harfbuzz',
    'freetype',
]


def _walk_filenames(root):
    names = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            names.append(os.path.join(dirpath, f))
    return names


def verify(dist_dir):
    problems = []

    if not os.path.isdir(dist_dir):
        return [f'dist folder not found: {dist_dir} (build it first with: pyinstaller desktop_app.spec)']

    internal_dir = os.path.join(dist_dir, '_internal')
    search_root = internal_dir if os.path.isdir(internal_dir) else dist_dir
    all_files = _walk_filenames(search_root)
    all_files_lower = [f.lower() for f in all_files]

    # 1. Bundled Tamil fonts must be present somewhere under the bundle.
    fonts_dir = os.path.join(search_root, 'backend', 'fonts')
    for font_name in REQUIRED_FONTS:
        expected = os.path.join(fonts_dir, font_name)
        found = os.path.isfile(expected) or any(font_name.lower() in f for f in all_files_lower)
        if not found:
            problems.append(
                f'Missing bundled font: {font_name} (expected at {expected}). '
                f"Tamil printing will raise 'Tamil thermal printing requires a Tamil font' "
                f'on any machine without Nirmala/Latha installed.'
            )

    # 2. Native shaping libraries (uharfbuzz, freetype) must have been collected.
    for hint in REQUIRED_NATIVE_LIB_HINTS:
        if not any(hint in f for f in all_files_lower):
            problems.append(
                f"No file containing '{hint}' found under {search_root}. "
                f'uharfbuzz/freetype ship native extensions as package data; '
                f'if missing, importing services.tamil_text_render will fail at runtime '
                f'and Tamil receipts will not print (or will fall back to scrambled output).'
            )

    return problems


def main():
    dist_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIST_DIR
    problems = verify(dist_dir)

    if not problems:
        print(f'OK: {dist_dir} looks correctly bundled for Tamil raster printing.')
        print(
            'Recommended final check (manual): run the built EXE, open '
            'http://localhost:5000/debug/tamil-raster in a browser, and confirm '
            'words like "திருமேனி" and "வாடிக்கையாளர்" render with vowel signs '
            'in the correct position (not scrambled).'
        )
        return 0

    print(f'FAILED: {len(problems)} issue(s) found in {dist_dir}:\n')
    for i, p in enumerate(problems, 1):
        print(f'  {i}. {p}')
    print(
        '\nMost likely fix: rebuild with the updated desktop_app.spec '
        '(pyinstaller --clean desktop_app.spec) after confirming uharfbuzz, '
        'freetype-py, and wcwidth are installed in the build environment '
        "(pip install uharfbuzz freetype-py wcwidth)."
    )
    return 1


if __name__ == '__main__':
    sys.exit(main())
