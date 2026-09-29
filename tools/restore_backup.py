"""
Restore billing.db from an automatic backup — DHANA DHANYA KADAI billing.

Run ONLY while the billing app is CLOSED.

    py -3.10 tools\\restore_backup.py --list               # show backups (newest first)
    py -3.10 tools\\restore_backup.py --verify-all         # integrity-check every backup
    py -3.10 tools\\restore_backup.py billing_20260928_101500.db   # restore that file
    py -3.10 tools\\restore_backup.py --latest             # restore newest valid backup

Safety:
  * refuses to run if the app answers on http://127.0.0.1:5000/health
  * integrity-checks the backup before touching anything
  * the current billing.db is first copied to db_backups/billing_before_restore_<ts>.db
  * the restore is written to a temp file, verified, then atomically swapped in
"""

import argparse
import os
import shutil
import sqlite3
import sys
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.normpath(os.path.join(HERE, '..', 'backend'))
if getattr(sys, 'frozen', False):
    BACKEND = os.path.dirname(sys.executable)
DB_PATH = os.path.join(BACKEND, 'billing.db')
BACKUP_DIR = os.path.join(BACKEND, 'db_backups')
REQUIRED_TABLES = ('products', 'bills', 'customers', 'app_settings')


def check(path):
    """Return (ok, message) for a candidate database file."""
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            res = con.execute('PRAGMA integrity_check').fetchone()[0]
            if res != 'ok':
                return False, f'integrity_check: {res}'
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in REQUIRED_TABLES if t not in tables]
            if missing:
                return False, f'missing tables: {missing}'
            counts = {t: con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                      for t in ('products', 'bills', 'customers')}
            return True, f"ok  products={counts['products']} bills={counts['bills']} customers={counts['customers']}"
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        return False, f'not a valid database: {e}'


def app_running():
    try:
        with urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=2):
            return True
    except Exception:
        return False


def backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    return sorted((f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')), reverse=True)


def restore(src):
    ok, msg = check(src)
    if not ok:
        sys.exit(f'REFUSED: backup failed verification ({msg}). Nothing changed.')
    if app_running():
        sys.exit('REFUSED: the billing app is running. Close it first. Nothing changed.')
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if os.path.exists(DB_PATH):
        safety = os.path.join(BACKUP_DIR, f'billing_before_restore_{stamp}.db')
        shutil.copy2(DB_PATH, safety)
        print(f'Current database saved to {safety}')
    tmp = DB_PATH + '.restore_tmp'
    shutil.copy2(src, tmp)
    ok, msg = check(tmp)
    if not ok:
        os.remove(tmp)
        sys.exit(f'REFUSED: copy failed verification ({msg}). Nothing changed.')
    for suffix in ('-wal', '-shm', '-journal'):
        stale = DB_PATH + suffix
        if os.path.exists(stale):
            os.remove(stale)
    os.replace(tmp, DB_PATH)
    print(f'RESTORED {os.path.basename(src)} -> {DB_PATH}  ({msg})')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('backup', nargs='?', help='backup file name inside db_backups/ (or a full path)')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--verify-all', action='store_true')
    ap.add_argument('--latest', action='store_true', help='restore the newest backup that verifies')
    a = ap.parse_args()

    if a.list or a.verify_all:
        for f in backups():
            line = f
            if a.verify_all:
                ok, msg = check(os.path.join(BACKUP_DIR, f))
                line += ('   OK   ' if ok else '   BAD  ') + msg
            print(line)
        return
    if a.latest:
        for f in backups():
            if f.startswith('billing_before_restore_'):
                continue
            p = os.path.join(BACKUP_DIR, f)
            if check(p)[0]:
                return restore(p)
        sys.exit('No valid backup found.')
    if a.backup:
        p = a.backup if os.path.isabs(a.backup) else os.path.join(BACKUP_DIR, a.backup)
        if not os.path.isfile(p):
            sys.exit(f'Not found: {p}')
        return restore(p)
    ap.print_help()


if __name__ == '__main__':
    main()
