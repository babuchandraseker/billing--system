"""
DHANA DHANYA KADAI – Desktop Launcher  (desktop_app.py)
========================================================
Starts Flask in a background thread, waits for server readiness
via /health (no auth required), then opens the PyWebView window
at /login. Flask redirects to / automatically if already authenticated.

PyInstaller compatible: resource_path() resolves bundled assets.
"""

import webview
import threading
import time
import sys
import os
import json
import socket
import urllib.request
import urllib.error


# ── PyInstaller-compatible path resolver ────────────────────────────────────
def resource_path(relative_path):
    """Return absolute path — works both in dev and inside a PyInstaller EXE."""
    try:
        base_path = sys._MEIPASS          # PyInstaller temp folder
    except AttributeError:
        # Dev: anchor to the folder containing this script (Billing_system_v7/)
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


# ── Add backend/ to sys.path so `from app import ...` works ─────────────────
# Do NOT os.chdir() — changing cwd breaks resource_path resolution in app.py.
BACKEND_DIR = resource_path("backend")
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# ── Persistent log file (AUDIT FIX: under pythonw there is no console, so
#    startup errors, print failures and backup failures were invisible). ─────
import logging
from logging.handlers import RotatingFileHandler

def _setup_file_logging():
    base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else BACKEND_DIR
    log_dir = os.path.join(base, 'logs')
    try:
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(os.path.join(log_dir, 'billing_app.log'),
                                      maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s %(name)s: %(message)s'))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        logging.getLogger('werkzeug').setLevel(logging.WARNING)   # no per-request noise
        return os.path.join(log_dir, 'billing_app.log')
    except OSError:
        return None

LOG_FILE = _setup_file_logging()
log = logging.getLogger('desktop')

# Import Flask app — app.py uses __file__-anchored paths so cwd doesn't matter
from app import app, init_db, start_backup_scheduler
log.info('Application starting (log file: %s)', LOG_FILE)


# ── Server settings ─────────────────────────────────────────────────────────
HOST       = "127.0.0.1"
PORT       = 5000
BASE_URL   = f"http://{HOST}:{PORT}"
HEALTH_URL = f"{BASE_URL}/health"   # Always unauthenticated — safe readiness probe
START_URL  = f"{BASE_URL}/login"    # Flask redirects to / when already authenticated
APP_NAME   = "DHANA DHANYA KADAI"
WINDOW_TITLE = "DHANA DHANYA KADAI – Billing System"


def _show_error(title, message):
    log.error("%s: %s", title, message.replace("\n", " "))
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        print(f"[FATAL] {title}: {message}")


# ── WebView2 runtime check ───────────────────────────────────────────────────
# pywebview silently falls back to the obsolete MSHTML (IE11) engine when the
# WebView2 runtime is missing, even with gui="edgechromium". This mirrors
# pywebview's own detection (webview/platforms/winforms.py:_is_chromium) so we
# refuse to start instead of running the POS in IE11.
_WEBVIEW2_CLIENT_KEYS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # Evergreen runtime
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # Beta
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # Dev
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",  # Canary
)
_WEBVIEW2_MIN_MAJOR = 86
_DOTNET_462_RELEASE = 394802


def check_webview2_runtime():
    """Return (ok, detail). Windows only."""
    import winreg
    from platform import machine
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as k:
            release = winreg.QueryValueEx(k, "Release")[0]
    except OSError:
        release = 0
    if release < _DOTNET_462_RELEASE:
        return False, ".NET Framework 4.6.2 or newer is not installed."
    for guid in _WEBVIEW2_CLIENT_KEYS:
        for hive in ("HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"):
            if machine() == "x86" or hive == "HKEY_CURRENT_USER":
                sub = rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"
            else:
                sub = rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"
            try:
                with winreg.OpenKey(getattr(winreg, hive), sub) as k:
                    build = str(winreg.QueryValueEx(k, "pv")[0])
            except OSError:
                continue
            try:
                major = int(build.split(".")[0])
            except ValueError:
                continue
            if major >= _WEBVIEW2_MIN_MAJOR:
                return True, build
    return False, "Microsoft Edge WebView2 Runtime is not installed."


# ── Port 5000 check ──────────────────────────────────────────────────────────
# Werkzeug binds with SO_REUSEADDR and calls sys.exit() inside the server
# thread when the bind fails, so a port conflict used to fail silently and
# /health could then be answered by a different process.
def port_status(host=HOST, port=PORT):
    """'free', 'self' (another copy of this app is serving), or 'busy'."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        listening = s.connect_ex((host, port)) == 0
    if not listening:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                s.bind((host, port))
            return "free"
        except OSError:
            return "busy"
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as resp:
            if json.load(resp).get("shop") == APP_NAME:
                return "self"
    except Exception:
        pass
    return "busy"


def _focus_existing_window():
    try:
        import ctypes
        hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def preflight_checks():
    if sys.platform.startswith("win"):
        ok, detail = check_webview2_runtime()
        if not ok:
            _show_error(
                "Microsoft Edge WebView2 Runtime Required",
                f"{APP_NAME} needs the Microsoft Edge WebView2 Runtime to run.\n\n"
                f"Problem: {detail}\n\n"
                "Install the \"Evergreen\" WebView2 Runtime from Microsoft:\n"
                "https://developer.microsoft.com/microsoft-edge/webview2/\n\n"
                "Then start the billing application again."
            )
            sys.exit(1)
        log.info("WebView2 runtime detected: %s", detail)

    status = port_status()
    if status == "self":
        log.info("Another instance is already running on port %s", PORT)
        if not _focus_existing_window():
            _show_error(
                "Already Running",
                f"{APP_NAME} is already running.\n\n"
                "Use the open billing window (check the taskbar)."
            )
        sys.exit(0)
    if status == "busy":
        _show_error(
            "Startup Error",
            f"Port {PORT} is being used by another program, so the billing "
            "server cannot start.\n\n"
            "Close the other program (or restart the computer) and try again."
            + (f"\n\nLog file: {LOG_FILE}" if LOG_FILE else "")
        )
        sys.exit(1)


preflight_checks()


# ── Flask server thread ──────────────────────────────────────────────────────
STARTUP_ERROR = None

def run_server():
    """Initialise DB then run Flask. Runs in a daemon thread.

    AUDIT FIX: a failed init_db() used to be logged as "non-fatal" and the POS
    opened on a broken database. Now the server does not start and the user
    gets an error dialog with the reason.
    """
    global STARTUP_ERROR
    try:
        init_db()
        start_backup_scheduler(interval_hours=1)
        log.info("DB initialised. Serving on %s", BASE_URL)
    except Exception as exc:
        STARTUP_ERROR = f"{type(exc).__name__}: {exc}"
        log.exception("Database initialisation FAILED — server not started")
        return
    try:
        app.run(
            host=HOST,
            port=PORT,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    except (OSError, SystemExit) as exc:
        # Werkzeug calls sys.exit(1) when the port cannot be bound.
        STARTUP_ERROR = f"Could not start the server on port {PORT} (it may be in use by another program)."
        log.exception("Flask server stopped")


# ── Splash Screen ────────────────────────────────────────────────────────────
splash = None
try:
    import tkinter as tk
    splash = tk.Tk()
    splash.overrideredirect(True)
    splash.attributes("-topmost", True)
    
    # Center splash screen
    sw = splash.winfo_screenwidth()
    sh = splash.winfo_screenheight()
    w, h = 420, 220
    x = (sw // 2) - (w // 2)
    y = (sh // 2) - (h // 2)
    splash.geometry(f"{w}x{h}+{x}+{y}")
    splash.configure(bg="#ffffff", highlightthickness=2, highlightbackground="#166534")
    
    tk.Label(splash, text="DHANA DHANYA KADAI", font=("Segoe UI", 18, "bold"), bg="#ffffff", fg="#166534").pack(pady=(50,10))
    tk.Label(splash, text="Loading Application...\nPlease wait...", font=("Segoe UI", 12), bg="#ffffff", fg="#4b5563").pack()
    
    splash.update()
except Exception:
    splash = None

flask_thread = threading.Thread(target=run_server, daemon=True, name="FlaskServer")
flask_thread.start()


# ── Wait for Flask /health to respond ───────────────────────────────────────
def wait_for_server(health_url: str, retries: int = 40, delay: float = 0.5, splash=None) -> bool:
    """
    Poll /health until Flask responds HTTP 200.
    Returns True if ready, False if timed out.
    """
    for attempt in range(retries):
        if STARTUP_ERROR or not flask_thread.is_alive():
            return False
        if splash:
            try:
                splash.update()
            except Exception:
                pass
        try:
            with urllib.request.urlopen(health_url, timeout=2) as resp:
                if resp.status == 200:
                    print(f"[OK] Flask server ready after {attempt * delay:.1f}s")
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(delay)
    print(f"[ERROR] Flask server did not start after {retries * delay:.0f}s")
    return False


if not wait_for_server(HEALTH_URL, splash=splash):
    if splash:
        try:
            splash.destroy()
        except:
            pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Startup Error",
            "The billing server failed to start.\n\n"
            + (f"Reason: {STARTUP_ERROR}\n\n" if STARTUP_ERROR else "")
            + (f"Log file: {LOG_FILE}\n\n" if LOG_FILE else "")
            + "Please check:\n"
            "  \u2022 Port 5000 is not already in use\n"
            "  \u2022 All application files are intact\n"
            "  \u2022 You have write access to the backend folder"
        )
        root.destroy()
    except Exception:
        print("[FATAL] Server failed to start.")
    sys.exit(1)


# ── Open PyWebView window (maximized for POS use) ───────────────────────────
# We launch the window already sized to the primary screen so it visually
# appears maximized from the very first frame (no flash-of-small-window),
# then call window.maximize() once the GUI loop is ready so the OS-native
# "maximized" state is set (taskbar visible, restore button works, etc.).
def _detect_screen_size(default=(1280, 820)):
    """Best-effort primary-screen size detection. Falls back to `default`
    if no display backend is available (e.g. headless build environment)."""
    # 1) Try tkinter (ships with CPython on Windows — no extra deps)
    try:
        import tkinter as _tk
        _r = _tk.Tk()
        _r.withdraw()
        w = _r.winfo_screenwidth()
        h = _r.winfo_screenheight()
        _r.destroy()
        if w >= 800 and h >= 600:
            return w, h
    except Exception:
        pass
    # 2) Windows: query via ctypes user32 (works even without tkinter)
    try:
        if sys.platform.startswith("win"):
            import ctypes
            user32 = ctypes.windll.user32
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
            w = user32.GetSystemMetrics(0)
            h = user32.GetSystemMetrics(1)
            if w >= 800 and h >= 600:
                return w, h
    except Exception:
        pass
    return default


_SCREEN_W, _SCREEN_H = _detect_screen_size()

window = webview.create_window(
    title=WINDOW_TITLE,
    url=START_URL,
    width=_SCREEN_W,
    height=_SCREEN_H,
    x=0,
    y=0,
    resizable=True,
    maximized=True,
    min_size=(1280, 720),
)

# Destroy the splash screen properly without mainloop
if splash:
    try:
        splash.withdraw()
        splash.update()
        splash.destroy()
    except Exception:
        pass

def on_loaded():
    try:
        window.maximize()
    except Exception:
        pass
    try:
        import ctypes
        import time
        # Small delay to ensure the window is mapped
        time.sleep(0.1)
        hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 3) # SW_MAXIMIZE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass

window.events.loaded += on_loaded

# Launch with EdgeChromium renderer (Windows 10/11 built-in — no extra install).
# maximized=True sets the OS-native maximized state from the first frame so
# the restore/maximise button in the title bar works correctly.
webview.start(gui="edgechromium", debug=False)

